#!/usr/bin/env python3
"""Build a minimal static MPD from yt-dlp info and proxy media assets for VLC."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import struct
import time
from typing import Any
from xml.dom.minidom import Document, Element

import aiohttp
from aiohttp import web
import yt_dlp


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ytmpd-server")

# ── caches ──────────────────────────────────────────────────────────────────────

_INFO_CACHE: dict[str, dict[str, Any]] = {}
_PROBE_CACHE: dict[str, dict[str, Any]] = {}  # key: "{video_id}_{format_id}"


def _parse_video_id(url: str) -> str:
    """Extract video ID from various YouTube URL formats."""
    import urllib.parse as urlparse

    parsed = urlparse.urlparse(url)
    query = urlparse.parse_qs(parsed.query)

    if "v" in query:
        return query["v"][0]

    # Match youtu.be/ID or youtube.com/embed/ID
    path = parsed.path.strip("/")
    segments = path.split("/")
    if len(segments) >= 2 and segments[0] == "embed":
        return segments[1]
    if len(segments) == 1 and len(segments[0]) == 11:
        return segments[0]

    # Short URL redirect: https://youtu.be/ID
    if parsed.hostname in ("youtu.be",):
        return path.split("?")[0]

    raise ValueError(f"unable to extract video ID from {url}")


def _find_expire(url: str) -> int | None:
    m = re.search(r"expire=(\d+)", url)
    return int(m.group(1)) if m else None


def _min_expire(info: dict[str, Any]) -> int | None:
    expires: list[int] = []
    for fmt in info.get("formats") or []:
        u = fmt.get("url") or ""
        e = _find_expire(u)
        if e:
            expires.append(e)
    return min(expires) if expires else None


def _get_or_fetch_info(youtube_url: str) -> dict[str, Any]:
    """Return cached info if not expired, otherwise fetch and cache."""
    video_id = _parse_video_id(youtube_url)
    now = int(time.time())

    if video_id in _INFO_CACHE:
        cached = _INFO_CACHE[video_id]
        if _min_expire(cached) and _min_expire(cached) > now:
            log.info("cache hit %s", video_id)
            return cached
        else:
            del _INFO_CACHE[video_id]

    info = _extract_info(youtube_url)
    exp = _min_expire(info)
    _INFO_CACHE[video_id] = info
    log.info("cached info for %s (expires=%s)", video_id, exp)
    return info


def _cleanup_expired() -> None:
    """Remove expired entries from info cache."""
    now = int(time.time())
    expired = [vid for vid, info in _INFO_CACHE.items() if _min_expire(info) and _min_expire(info) <= now]
    for vid in expired:
        del _INFO_CACHE[vid]
        log.info("evicted expired cache for %s", vid)


async def _probe_selected_formats(selected: dict[str, Any], video_id: str) -> dict[str, dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }
    probes: dict[str, dict[str, Any]] = {}
    failed: list[str] = []

    async def _do_probe(session: aiohttp.ClientSession, fmt: dict[str, Any]) -> dict[str, Any] | None:
        cache_key = f"{video_id}_{fmt['format_id']}"
        if cache_key in _PROBE_CACHE:
            log.info("probe cache hit %s", fmt["format_id"])
            return _PROBE_CACHE[cache_key]

        if fmt.get("ext") not in ("mp4", "m4a", "webm"):
            return None

        for attempt in range(5):
            try:
                probe = await _fetch_mp4_probe(session, fmt["url"])
                if probe:
                    _PROBE_CACHE[cache_key] = probe
                    log.info("probed format=%s ranges=%s", fmt["format_id"], probe)
                return probe
            except Exception as e:
                log.warning("probe attempt %d failed format=%s: %s", attempt + 1, fmt["format_id"], e)
        return None

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = []
        for stream_name in ("video", "audio"):
            fmt = selected[stream_name]
            tasks.append(_do_probe(session, fmt))

        results = await asyncio.gather(*tasks)

        for stream_name, probe in zip(("video", "audio"), results):
            fmt = selected[stream_name]
            if probe:
                probes[str(fmt["format_id"])] = probe
            else:
                failed.append(fmt["format_id"])

    if failed:
        raise RuntimeError(f"probe failed for formats: {', '.join(failed)}")

    return probes


# ── helpers ─────────────────────────────────────────────────────────────────────


def _get_proxy() -> str | None:
    return os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")


def _extract_info(youtube_url: str) -> dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(youtube_url, download=False)


def _is_video_only(fmt: dict[str, Any]) -> bool:
    return fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") == "none"


def _is_audio_only(fmt: dict[str, Any]) -> bool:
    return fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") == "none"


def _video_score(fmt: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fmt.get("height") or 0,
        fmt.get("fps") or 0,
        fmt.get("tbr") or 0,
    )


def _audio_score(fmt: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fmt.get("asr") or 0,
        fmt.get("audio_channels") or 0,
        fmt.get("tbr") or 0,
    )


def _choose_av_pair(info: dict[str, Any]) -> dict[str, Any]:
    formats = info.get("formats") or []

    families = (
        (
            "avc",
            lambda f: _is_video_only(f) and f.get("ext") == "mp4" and not str(f.get("vcodec") or "").startswith("av01"),
            lambda f: _is_audio_only(f) and f.get("ext") == "m4a" and str(f.get("acodec") or "").startswith("mp4a"),
        ),
        (
            "vp9",
            lambda f: _is_video_only(f) and f.get("ext") == "webm" and str(f.get("vcodec") or "").startswith("vp9"),
            lambda f: _is_audio_only(f) and f.get("ext") == "m4a" and str(f.get("acodec") or "").startswith("mp4a"),
        ),
        (
            "vp9_mp4a",
            lambda f: _is_video_only(f) and f.get("ext") == "mp4" and str(f.get("vcodec") or "").startswith("vp9"),
            lambda f: _is_audio_only(f) and f.get("ext") == "m4a" and str(f.get("acodec") or "").startswith("mp4a"),
        ),
    )

    for family, video_pred, audio_pred in families:
        videos = [fmt for fmt in formats if video_pred(fmt)]
        audios = [fmt for fmt in formats if audio_pred(fmt)]
        if videos and audios:
            video = max(videos, key=_video_score)
            audio = max(audios, key=_audio_score)
            return {"family": family, "video": video, "audio": audio}

    videos = [fmt for fmt in formats if _is_video_only(fmt)]
    audios = [fmt for fmt in formats if _is_audio_only(fmt)]
    if not videos or not audios:
        raise RuntimeError("Could not find separate video/audio formats in info")

    return {
        "family": "fallback",
        "video": max(videos, key=_video_score),
        "audio": max(audios, key=_audio_score),
    }


def _format_duration(seconds: float | int | None) -> str:
    total = float(seconds or 0)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total - hours * 3600 - minutes * 60
    if secs.is_integer():
        sec_text = str(int(secs))
    else:
        sec_text = f"{secs:.3f}".rstrip("0").rstrip(".")
    if hours:
        return f"PT{hours}H{minutes}M{sec_text}S"
    if minutes:
        return f"PT{minutes}M{sec_text}S"
    return f"PT{sec_text}S"


def _mime_type(fmt: dict[str, Any]) -> str:
    if _is_video_only(fmt):
        if str(fmt.get("vcodec") or "").startswith("av01"):
            return "video/webm"
        return "video/mp4" if fmt.get("ext") == "mp4" else "video/webm"
    return "audio/mp4" if fmt.get("ext") == "m4a" else "audio/webm"


def _bandwidth(fmt: dict[str, Any]) -> str:
    tbr = fmt.get("tbr") or 0
    return str(max(1, int(float(tbr) * 1000)))


# ── subtitle helpers ──────────────────────────────────────────────────────────────


def _pick_vtt_subtitles(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract one vtt subtitle track per language from manually uploaded subtitles.
    Fallback to automatic_captions if no manual subtitles exist."""
    # Prefer manually uploaded subtitles (not auto-generated)
    subtitles = info.get("subtitles") or {}
    if not subtitles:
        # Fall back to auto-generated
        subtitles = info.get("automatic_captions") or {}

    result: list[dict[str, Any]] = []
    for lang, variants in subtitles.items():
        for variant in variants:
            if variant.get("ext") == "vtt":
                result.append({
                    "language": lang,
                    "name": variant.get("name", lang),
                    "url": variant["url"],
                    "ext": "vtt",
                })
                break

    return sorted(result, key=lambda s: s["language"])


def _parse_mp4_boxes(buf: bytes) -> list[dict[str, int | str]]:
    boxes: list[dict[str, int | str]] = []
    offset = 0
    size_buf = len(buf)

    while offset + 8 <= size_buf:
        size = struct.unpack_from(">I", buf, offset)[0]
        box_type = buf[offset + 4: offset + 8].decode("ascii", errors="replace")
        header_size = 8

        if size == 1:
            if offset + 16 > size_buf:
                break
            size = struct.unpack_from(">Q", buf, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = size_buf - offset

        if size < header_size or offset + size > size_buf:
            break

        boxes.append(
            {
                "type": box_type,
                "start": offset,
                "end": offset + size - 1,
                "size": size,
            }
        )
        offset += size

    return boxes


def _parse_sidx(buf: bytes, box_start: int, box_size: int) -> dict[str, Any] | None:
    if box_start + box_size > len(buf) or box_size < 32:
        return None

    version = buf[box_start + 8]
    pos = box_start + 12
    if pos + 8 > box_start + box_size:
        return None

    _reference_id = struct.unpack_from(">I", buf, pos)[0]
    pos += 4
    timescale = struct.unpack_from(">I", buf, pos)[0]
    pos += 4

    if version == 0:
        if pos + 8 > box_start + box_size:
            return None
        earliest_presentation_time = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        first_offset = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
    elif version == 1:
        if pos + 16 > box_start + box_size:
            return None
        earliest_presentation_time = struct.unpack_from(">Q", buf, pos)[0]
        pos += 8
        first_offset = struct.unpack_from(">Q", buf, pos)[0]
        pos += 8
    else:
        return None

    if pos + 4 > box_start + box_size:
        return None
    pos += 2  # reserved
    reference_count = struct.unpack_from(">H", buf, pos)[0]
    pos += 2

    next_offset = box_start + box_size + first_offset
    segments: list[dict[str, int]] = []

    for _ in range(reference_count):
        if pos + 12 > box_start + box_size:
            return None

        ref_info = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        ref_type = (ref_info >> 31) & 0x1
        ref_size = ref_info & 0x7FFFFFFF
        subsegment_duration = struct.unpack_from(">I", buf, pos)[0]
        pos += 4
        sap_info = struct.unpack_from(">I", buf, pos)[0]
        pos += 4

        if ref_type != 0:
            return None

        segments.append(
            {
                "range_start": next_offset,
                "range_end": next_offset + ref_size - 1,
                "duration": subsegment_duration,
                "starts_with_sap": (sap_info >> 31) & 0x1,
            }
        )
        next_offset += ref_size

    return {
        "timescale": timescale,
        "earliest_presentation_time": earliest_presentation_time,
        "first_offset": first_offset,
        "segments": segments,
    }


async def _fetch_mp4_probe(
    session: aiohttp.ClientSession,
    url: str,
    *,
    probe_bytes: int = 512 * 1024,
) -> dict[str, str] | None:
    headers = {"Range": f"bytes=0-{probe_bytes - 1}"}
    proxy = _get_proxy()

    async with session.get(url, proxy=proxy, headers=headers) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(text or f"probe request failed: {resp.status}")
        body = await resp.read()

    boxes = _parse_mp4_boxes(body)
    if not boxes:
        return None

    moov = next((box for box in boxes if box["type"] == "moov"), None)
    if not moov:
        return None

    sidx = next((box for box in boxes if box["type"] == "sidx"), None)
    init_range = f"0-{moov['end']}"
    result: dict[str, Any] = {"init_range": init_range}
    if sidx:
        result["index_range"] = f"{sidx['start']}-{sidx['end']}"
        sidx_info = _parse_sidx(body, int(sidx["start"]), int(sidx["size"]))
        if sidx_info:
            result["sidx"] = sidx_info
    return result


def _representation_to_dom(
    doc: Document,
    adaptation_set_node: Element,
    fmt: dict[str, Any],
    *,
    audio_lang: str | None = None,
    segment_probe: dict[str, Any] | None = None,
) -> None:
    vcodec = str(fmt.get("vcodec") if _is_video_only(fmt) else fmt.get("acodec"))
    bandwidth = int(_bandwidth(fmt))

    repr_node = doc.createElement("Representation")
    repr_node.setAttribute("id", str(fmt["format_id"]))
    repr_node.setAttribute("mimeType", _mime_type(fmt))
    repr_node.setAttribute("codecs", vcodec)
    repr_node.setAttribute("bandwidth", str(bandwidth))
    repr_node.setAttribute("startWithSAP", "1")
    if fmt.get("width"):
        repr_node.setAttribute("width", str(fmt["width"]))
    if fmt.get("height"):
        repr_node.setAttribute("height", str(fmt["height"]))
    if fmt.get("fps"):
        fps = fmt["fps"]
        repr_node.setAttribute("frameRate", str(int(fps) if float(fps).is_integer() else fps))
    if fmt.get("asr"):
        repr_node.setAttribute("audioSamplingRate", str(fmt["asr"]))

    base_url_node = doc.createElement("BaseURL")
    base_url_node.appendChild(doc.createTextNode(fmt["url"]))
    repr_node.appendChild(base_url_node)

    sidx_info = segment_probe.get("sidx") if segment_probe else None
    if sidx_info and sidx_info.get("segments"):
        segment_list_node = doc.createElement("SegmentList")
        segment_list_node.setAttribute("timescale", str(sidx_info["timescale"]))
        if segment_probe and segment_probe.get("init_range"):
            init_node = doc.createElement("Initialization")
            init_node.setAttribute("range", segment_probe["init_range"])
            segment_list_node.appendChild(init_node)

        sidx_segments = sidx_info["segments"]
        if len(sidx_segments) == 1:
            seg = sidx_segments[0]
            s_node = doc.createElement("S")
            s_node.setAttribute("d", str(int(seg["duration"])))
            s_node.setAttribute("initializationSegmentIndex", "0")
            s_node.setAttribute("index", "0")
            segment_list_node.appendChild(s_node)
            seg_url_node = doc.createElement("SegmentURL")
            seg_url_node.setAttribute("mediaRange", f"{seg['range_start']}-{seg['range_end']}")
            segment_list_node.appendChild(seg_url_node)
        else:
            timeline_node = doc.createElement("SegmentTimeline")
            for i, seg in enumerate(sidx_segments):
                s_node = doc.createElement("S")
                s_node.setAttribute("d", str(int(seg["duration"])))
                if i == 0:
                    s_node.setAttribute("t", "0")
                timeline_node.appendChild(s_node)
            segment_list_node.appendChild(timeline_node)
            for seg in sidx_segments:
                seg_url_node = doc.createElement("SegmentURL")
                seg_url_node.setAttribute("mediaRange", f"{seg['range_start']}-{seg['range_end']}")
                segment_list_node.appendChild(seg_url_node)

        repr_node.appendChild(segment_list_node)
    else:
        segment_base_node = doc.createElement("SegmentBase")
        if segment_probe and segment_probe.get("index_range"):
            segment_base_node.setAttribute("indexRange", segment_probe["index_range"])
        if segment_probe and segment_probe.get("init_range"):
            init_node = doc.createElement("Initialization")
            init_node.setAttribute("range", segment_probe["init_range"])
            segment_base_node.appendChild(init_node)
        repr_node.appendChild(segment_base_node)

    if _is_audio_only(fmt) and fmt.get("audio_channels"):
        ach_node = doc.createElement("AudioChannelConfiguration")
        ach_node.setAttribute("schemeIdUri", "urn:mpeg:dash:23003:3:audio_channel_configuration:2011")
        ach_node.setAttribute("value", str(fmt["audio_channels"]))
        repr_node.appendChild(ach_node)

    if audio_lang:
        repr_node.setAttribute("lang", audio_lang)

    adaptation_set_node.appendChild(repr_node)


def _subtitles_to_dom(
    doc: Document,
    period_node: Element,
    subtitles: list[dict[str, Any]],
) -> None:
    for sub in subtitles:
        text_set_node = doc.createElement("AdaptationSet")
        text_set_node.setAttribute("id", f"sub_{sub['language']}")
        text_set_node.setAttribute("contentType", "text")
        text_set_node.setAttribute("mimeType", "text/vtt")
        text_set_node.setAttribute("lang", sub["language"])

        repr_node = doc.createElement("Representation")
        repr_node.setAttribute("id", f"sub_{sub['language']}")
        repr_node.setAttribute("bandwidth", "1000")

        base_url_node = doc.createElement("BaseURL")
        base_url_node.appendChild(doc.createTextNode(sub["url"]))
        repr_node.appendChild(base_url_node)

        text_set_node.appendChild(repr_node)
        period_node.appendChild(text_set_node)


def build_mpd(
    info: dict[str, Any],
    selected: dict[str, Any],
    probes: dict[str, dict[str, Any]],
) -> bytes:
    video = selected["video"]
    audio = selected["audio"]
    duration = _format_duration(info.get("duration"))

    doc = Document()
    mpd_node = doc.createElement("MPD")
    mpd_node.setAttribute("xmlns", "urn:mpeg:dash:schema:mpd:2011")
    mpd_node.setAttribute("type", "static")
    mpd_node.setAttribute("profiles", "urn:mpeg:dash:profile:full:2011")
    mpd_node.setAttribute("mediaPresentationDuration", duration)
    mpd_node.setAttribute("minBufferTime", "PT1.5S")

    program_info_node = doc.createElement("ProgramInformation")
    title_node = doc.createElement("Title")
    title_node.appendChild(doc.createTextNode(info.get("title") or "untitled"))
    program_info_node.appendChild(title_node)
    mpd_node.appendChild(program_info_node)

    period_node = doc.createElement("Period")
    period_node.setAttribute("id", "p0")
    period_node.setAttribute("start", "PT0S")
    period_node.setAttribute("duration", duration)

    # video AdaptationSet
    video_set_node = doc.createElement("AdaptationSet")
    video_set_node.setAttribute("id", "video")
    video_set_node.setAttribute("contentType", "video")
    _representation_to_dom(
        doc,
        video_set_node,
        video,
        segment_probe=probes.get(str(video["format_id"])),
    )
    period_node.appendChild(video_set_node)

    # audio AdaptationSet
    audio_set_node = doc.createElement("AdaptationSet")
    audio_set_node.setAttribute("id", "audio")
    audio_set_node.setAttribute("contentType", "audio")
    if audio.get("language"):
        audio_set_node.setAttribute("lang", str(audio["language"]))
    _representation_to_dom(
        doc,
        audio_set_node,
        audio,
        segment_probe=probes.get(str(audio["format_id"])),
    )
    period_node.appendChild(audio_set_node)

    # subtitles
    subtitles = _pick_vtt_subtitles(info)
    _subtitles_to_dom(doc, period_node, subtitles)

    # comment
    comment_node = doc.createComment(
        f"selected family={selected['family']} video={video['format_id']} audio={audio['format_id']}"
    )
    mpd_node.appendChild(comment_node)

    mpd_node.appendChild(period_node)
    doc.appendChild(mpd_node)
    return doc.toprettyxml(indent="    ", encoding="utf-8")


async def _resolve_info(request: web.Request) -> tuple[dict[str, Any], str]:
    youtube_url = request.query.get("url")
    if not youtube_url:
        raise web.HTTPBadRequest(text="missing ?url=<youtube_url>")
    info = await asyncio.to_thread(_get_or_fetch_info, youtube_url)
    return info, _parse_video_id(youtube_url)


async def info_handler(request: web.Request) -> web.Response:
    info, video_id = await _resolve_info(request)
    selected = _choose_av_pair(info)
    probes = await _probe_selected_formats(selected, video_id)
    subtitles = _pick_vtt_subtitles(info)
    return web.json_response(
        {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "video_id": info.get("id"),
            "selected_family": selected["family"],
            "video": {
                key: selected["video"].get(key)
                for key in ("format_id", "ext", "vcodec", "width", "height", "fps", "tbr", "filesize", "url")
            },
            "audio": {
                key: selected["audio"].get(key)
                for key in ("format_id", "ext", "acodec", "asr", "audio_channels", "tbr", "filesize", "language", "url")
            },
            "subtitles": subtitles,
            "probes": probes,
            "manifest_url": str(request.url.with_path("/manifest").with_query(request.query)),
        }
    )


async def manifest_handler(request: web.Request) -> web.Response:
    log.info("GET /manifest %s", request.query.get("url"))
    try:
        info, video_id = await _resolve_info(request)
        selected = _choose_av_pair(info)
        probes = await _probe_selected_formats(selected, video_id)
        mpd = build_mpd(info, selected, probes)
    except RuntimeError as e:
        log.error("manifest error: %s", e)
        return web.Response(text=str(e), status=502)
    except Exception:
        log.exception("manifest error")
        return web.Response(text="Internal error", status=502)
    return web.Response(
        body=mpd,
        content_type="application/dash+xml",
        headers={
            "Cache-Control": "no-store",
            "X-YouTube-Title": info.get("title") or "",
            "X-YouTube-ID": info.get("id") or "",
        },
    )


async def play_handler(request: web.Request) -> web.Response:
    raise web.HTTPFound(str(request.url.with_path("/manifest").with_query(request.query)))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthesize a simple MPD from yt-dlp info.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8288")))
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    app = web.Application()
    app.router.add_get("/info", info_handler)
    app.router.add_get("/manifest", manifest_handler)
    app.router.add_get("/play", play_handler)

    log.info("starting MPD server on http://%s:%d", args.host, args.port)

    async def _periodic_cleanup(app):
        while True:
            now = time.time()
            exps = [_min_expire(info) for info in _INFO_CACHE.values()]
            exps = [e for e in exps if e]
            if exps:
                next_exp = min(exps)
                sleep_secs = max(next_exp - now, 0)
                log.debug("next cache expiry in %.0fs", sleep_secs)
                await asyncio.sleep(sleep_secs)
                _cleanup_expired()
            else:
                await asyncio.sleep(60)

    async def _on_startup(app):
        asyncio.create_task(_periodic_cleanup(app))

    app.on_startup.append(_on_startup)
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()

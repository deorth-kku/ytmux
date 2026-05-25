#!/usr/bin/env python3
"""Build a minimal static MPD from yt-dlp info and proxy media assets for VLC."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
import yt_dlp


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ytmpd-synth-poc")


def _get_proxy() -> str | None:
    return os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")


def _load_info_file(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


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
            "mp4",
            lambda f: _is_video_only(f) and f.get("ext") == "mp4" and not str(f.get("vcodec") or "").startswith("av01"),
            lambda f: _is_audio_only(f) and f.get("ext") == "m4a" and str(f.get("acodec") or "").startswith("mp4a"),
        ),
        (
            "webm",
            lambda f: _is_video_only(f) and f.get("ext") == "webm" and str(f.get("vcodec") or "").startswith("vp9"),
            lambda f: _is_audio_only(f) and f.get("ext") == "webm" and "opus" in str(f.get("acodec") or ""),
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
        "family": "mixed",
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
        return "video/mp4" if fmt.get("ext") == "mp4" else "video/webm"
    return "audio/mp4" if fmt.get("ext") == "m4a" else "audio/webm"


def _bandwidth(fmt: dict[str, Any]) -> str:
    tbr = fmt.get("tbr") or 0
    return str(max(1, int(float(tbr) * 1000)))


def _asset_url(request: web.Request, target_url: str) -> str:
    return str(request.url.with_path("/asset").with_query({"target": target_url}))


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


async def _probe_selected_formats(selected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }
    probes: dict[str, dict[str, Any]] = {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for stream_name in ("video", "audio"):
            fmt = selected[stream_name]
            if fmt.get("ext") not in ("mp4", "m4a"):
                continue
            probe = await _fetch_mp4_probe(session, fmt["url"])
            if probe:
                probes[str(fmt["format_id"])] = probe
                log.info("probed format=%s ranges=%s", fmt["format_id"], probe)

    return probes


def _representation_element(
    parent: ET.Element,
    request: web.Request,
    fmt: dict[str, Any],
    *,
    audio_lang: str | None = None,
    segment_probe: dict[str, Any] | None = None,
) -> None:
    attrs = {
        "id": str(fmt["format_id"]),
        "mimeType": _mime_type(fmt),
        "codecs": str(fmt.get("vcodec") if _is_video_only(fmt) else fmt.get("acodec")),
        "bandwidth": _bandwidth(fmt),
        "startWithSAP": "1",
    }
    if fmt.get("width"):
        attrs["width"] = str(fmt["width"])
    if fmt.get("height"):
        attrs["height"] = str(fmt["height"])
    if fmt.get("fps"):
        fps = fmt["fps"]
        attrs["frameRate"] = str(int(fps) if float(fps).is_integer() else fps)
    if fmt.get("asr"):
        attrs["audioSamplingRate"] = str(fmt["asr"])

    representation = ET.SubElement(parent, "Representation", attrs)
    ET.SubElement(representation, "BaseURL").text = _asset_url(request, fmt["url"])
    sidx_info = segment_probe.get("sidx") if segment_probe else None

    if sidx_info and sidx_info.get("segments"):
        segment_list_attrs = {"timescale": str(sidx_info["timescale"])}
        segment_list = ET.SubElement(representation, "SegmentList", segment_list_attrs)
        if segment_probe and segment_probe.get("init_range"):
            ET.SubElement(segment_list, "Initialization", {"range": segment_probe["init_range"]})
        timeline = ET.SubElement(segment_list, "SegmentTimeline")
        for seg in sidx_info["segments"]:
            ET.SubElement(timeline, "S", {"d": str(seg["duration"])})
            ET.SubElement(
                segment_list,
                "SegmentURL",
                {"mediaRange": f"{seg['range_start']}-{seg['range_end']}"},
            )
    else:
        segment_base_attrs: dict[str, str] = {}
        if segment_probe and segment_probe.get("index_range"):
            segment_base_attrs["indexRange"] = segment_probe["index_range"]
        segment_base = ET.SubElement(representation, "SegmentBase", segment_base_attrs)
        if segment_probe and segment_probe.get("init_range"):
            ET.SubElement(segment_base, "Initialization", {"range": segment_probe["init_range"]})

    if _is_audio_only(fmt) and fmt.get("audio_channels"):
        ET.SubElement(
            representation,
            "AudioChannelConfiguration",
            {
                "schemeIdUri": "urn:mpeg:dash:23003:3:audio_channel_configuration:2011",
                "value": str(fmt["audio_channels"]),
            },
        )
    if audio_lang:
        representation.set("lang", audio_lang)


def build_mpd(
    info: dict[str, Any],
    request: web.Request,
    selected: dict[str, Any],
    probes: dict[str, dict[str, Any]],
) -> bytes:
    video = selected["video"]
    audio = selected["audio"]
    duration = _format_duration(info.get("duration"))

    mpd = ET.Element(
        "MPD",
        {
            "xmlns": "urn:mpeg:dash:schema:mpd:2011",
            "type": "static",
            "profiles": "urn:mpeg:dash:profile:full:2011",
            "mediaPresentationDuration": duration,
            "minBufferTime": "PT1.5S",
        },
    )

    program_info = ET.SubElement(mpd, "ProgramInformation")
    ET.SubElement(program_info, "Title").text = info.get("title") or "untitled"

    period = ET.SubElement(mpd, "Period", {"id": "p0", "start": "PT0S", "duration": duration})
    video_set = ET.SubElement(period, "AdaptationSet", {"id": "video", "contentType": "video"})
    audio_attrs = {"id": "audio", "contentType": "audio"}
    if audio.get("language"):
        audio_attrs["lang"] = str(audio["language"])
    audio_set = ET.SubElement(period, "AdaptationSet", audio_attrs)

    _representation_element(
        video_set,
        request,
        video,
        segment_probe=probes.get(str(video["format_id"])),
    )
    _representation_element(
        audio_set,
        request,
        audio,
        segment_probe=probes.get(str(audio["format_id"])),
    )

    comment = ET.Comment(
        f"selected family={selected['family']} video={video['format_id']} audio={audio['format_id']}"
    )
    mpd.insert(0, comment)
    return ET.tostring(mpd, encoding="utf-8", xml_declaration=True)


async def _resolve_info(request: web.Request) -> dict[str, Any]:
    fixed_info_path = request.app["info_file"]
    if fixed_info_path:
        return await asyncio.to_thread(_load_info_file, fixed_info_path)

    youtube_url = request.query.get("url")
    if not youtube_url:
        raise web.HTTPBadRequest(text="missing ?url=<youtube_url>")
    return await asyncio.to_thread(_extract_info, youtube_url)


async def info_handler(request: web.Request) -> web.Response:
    info = await _resolve_info(request)
    selected = _choose_av_pair(info)
    probes = await _probe_selected_formats(selected)
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
            "probes": probes,
            "manifest_url": str(request.url.with_path("/manifest").with_query(request.query)),
        }
    )


async def manifest_handler(request: web.Request) -> web.Response:
    info = await _resolve_info(request)
    selected = _choose_av_pair(info)
    probes = await _probe_selected_formats(selected)
    mpd = build_mpd(info, request, selected, probes)
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


async def asset_handler(request: web.Request) -> web.StreamResponse:
    target = request.query.get("target")
    range_header = request.headers.get("Range")
    log.info("GET /asset target=%s range=%s", target, range_header)
    if not target:
        return web.Response(text="missing ?target=<upstream_url>", status=400)

    proxy = _get_proxy()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    }
    if range_header:
        headers["Range"] = range_header

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(target, proxy=proxy) as upstream:
            if upstream.status >= 400:
                text = await upstream.text()
                return web.Response(
                    text=text or f"upstream asset error: {upstream.status}",
                    status=upstream.status,
                )

            resp = web.StreamResponse(status=upstream.status)
            for name in (
                "Content-Type",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
                "ETag",
                "Last-Modified",
            ):
                value = upstream.headers.get(name)
                if value:
                    resp.headers[name] = value
            resp.headers["Cache-Control"] = "no-store"
            await resp.prepare(request)

            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synthesize a simple MPD from yt-dlp info.")
    parser.add_argument("--info-file", help="Use a local yt-dlp info JSON instead of fetching from YouTube.")
    parser.add_argument("--print-mpd", action="store_true", help="Print a sample MPD built from --info-file.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8288", help="Base URL used with --print-mpd.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8288")))
    return parser


def _fake_request(base_url: str) -> web.Request:
    class _FakeURL:
        def __init__(self, url: str):
            self._url = url

        def with_path(self, path: str) -> "_FakeURL":
            from yarl import URL
            return _FakeURL(str(URL(self._url).with_path(path).with_query(None)))

        def with_query(self, query: dict[str, str]) -> str:
            from yarl import URL
            return str(URL(self._url).with_query(query))

        def __str__(self) -> str:
            return self._url

    class _FakeRequest:
        def __init__(self, url: str):
            self.url = _FakeURL(url)

    return _FakeRequest(base_url)  # type: ignore[return-value]


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.print_mpd:
        if not args.info_file:
            raise SystemExit("--print-mpd requires --info-file")
        info = _load_info_file(args.info_file)
        selected = _choose_av_pair(info)
        print(build_mpd(info, _fake_request(args.base_url), selected, {}).decode("utf-8"))
        return

    app = web.Application()
    app["info_file"] = args.info_file
    app.router.add_get("/info", info_handler)
    app.router.add_get("/manifest", manifest_handler)
    app.router.add_get("/play", play_handler)
    app.router.add_get("/asset", asset_handler)

    log.info("starting synthesized MPD POC on http://%s:%d", args.host, args.port)
    if args.info_file:
        log.info("using fixed info file: %s", args.info_file)
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()

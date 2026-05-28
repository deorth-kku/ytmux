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

_VIDEO_FAMILY_ALIASES: dict[str, str] = {
    "avc": "avc",
    "h264": "avc",
    "mp4": "avc",
    "vp9": "vp9",
    "av1": "av1",
}

_AUDIO_FAMILY_ALIASES: dict[str, str] = {
    "aac": "aac",
    "m4a": "aac",
    "mp4a": "aac",
    "opus": "opus",
    "webm": "opus",
}


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
        cached_expire = _min_expire(cached)
        if cached_expire and cached_expire > now:
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
    expired = []
    for vid, info in _INFO_CACHE.items():
        expire = _min_expire(info)
        if expire and expire <= now:
            expired.append(vid)
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

        ext = str(fmt.get("ext") or "")
        if ext not in ("mp4", "m4a", "webm"):
            return None

        for attempt in range(5):
            try:
                if ext == "webm":
                    probe = await _fetch_webm_probe(session, fmt["url"])
                else:
                    probe = await _fetch_mp4_probe(session, fmt["url"])
                if probe:
                    if fmt.get("duration"):
                        probe["duration"] = float(fmt["duration"])
                    elif fmt.get("filesize") and fmt.get("tbr"):
                        probe["duration"] = max(0.1, (float(fmt["filesize"]) * 8.0) / (float(fmt["tbr"]) * 1000.0))
                    _PROBE_CACHE[cache_key] = probe
                    log.info("probed format=%s ranges=%s", fmt["format_id"], probe)
                else:
                    log.warning(
                        "probe attempt %d returned no probe format=%s ext=%s url=%s",
                        attempt + 1,
                        fmt["format_id"],
                        ext,
                        fmt["url"],
                    )
                return probe
            except Exception as e:
                log.warning(
                    "probe attempt %d failed format=%s ext=%s error_type=%s error=%r",
                    attempt + 1,
                    fmt["format_id"],
                    ext,
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
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
                failed.append(str(fmt["format_id"]))

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


def _canonical_video_family(value: str | None) -> str:
    if not value:
        return "avc"
    family = _VIDEO_FAMILY_ALIASES.get(value.lower())
    if not family:
        raise web.HTTPBadRequest(text="invalid ?video= value; expected avc|vp9|av1 or ?video_format_id=")
    return family


def _canonical_audio_family(value: str | None) -> str:
    if not value:
        return "aac"
    family = _AUDIO_FAMILY_ALIASES.get(value.lower())
    if not family:
        raise web.HTTPBadRequest(text="invalid ?audio= value; expected aac|opus or ?audio_format_id=")
    return family


def _matches_video_family(fmt: dict[str, Any], family: str) -> bool:
    if not _is_video_only(fmt):
        return False
    vcodec = str(fmt.get("vcodec") or "")
    ext = str(fmt.get("ext") or "")
    if family == "avc":
        return ext == "mp4" and vcodec.startswith("avc1")
    if family == "vp9":
        return ext == "webm" and vcodec.startswith("vp9")
    if family == "av1":
        return ext == "webm" and vcodec.startswith("av01")
    return False


def _matches_audio_family(fmt: dict[str, Any], family: str) -> bool:
    if not _is_audio_only(fmt):
        return False
    acodec = str(fmt.get("acodec") or "")
    ext = str(fmt.get("ext") or "")
    if family == "aac":
        return ext == "m4a" and acodec.startswith("mp4a")
    if family == "opus":
        return ext == "webm" and acodec.startswith("opus")
    return False


def _find_format_by_id(info: dict[str, Any], format_id: str) -> dict[str, Any] | None:
    for fmt in info.get("formats") or []:
        if str(fmt.get("format_id")) == format_id:
            return fmt
    return None


def _choose_best_format(formats: list[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    if not formats:
        raise RuntimeError(f"Could not find separate {kind} formats in info")
    if kind == "video":
        return max(formats, key=_video_score)
    return max(formats, key=_audio_score)


def _list_selectable_formats(info: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    video_formats = []
    audio_formats = []
    for fmt in info.get("formats") or []:
        if _is_video_only(fmt) and str(fmt.get("ext") or "") in ("mp4", "webm"):
            video_formats.append(
                {
                    "format_id": fmt.get("format_id"),
                    "ext": fmt.get("ext"),
                    "vcodec": fmt.get("vcodec"),
                    "width": fmt.get("width"),
                    "height": fmt.get("height"),
                    "fps": fmt.get("fps"),
                    "tbr": fmt.get("tbr"),
                }
            )
        if _is_audio_only(fmt) and str(fmt.get("ext") or "") in ("m4a", "webm"):
            audio_formats.append(
                {
                    "format_id": fmt.get("format_id"),
                    "ext": fmt.get("ext"),
                    "acodec": fmt.get("acodec"),
                    "asr": fmt.get("asr"),
                    "audio_channels": fmt.get("audio_channels"),
                    "tbr": fmt.get("tbr"),
                    "language": fmt.get("language"),
                }
            )
    video_formats.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
    audio_formats.sort(key=lambda f: (f.get("asr") or 0, f.get("audio_channels") or 0, f.get("tbr") or 0), reverse=True)
    return {"video": video_formats, "audio": audio_formats}


def _choose_av_pair(
    info: dict[str, Any],
    *,
    video_selector: str | None = None,
    audio_selector: str | None = None,
    video_format_id: str | None = None,
    audio_format_id: str | None = None,
) -> dict[str, Any]:
    formats = info.get("formats") or []

    if video_format_id:
        video = _find_format_by_id(info, video_format_id)
        if not video or not _is_video_only(video):
            raise web.HTTPBadRequest(text=f"unknown or non-video ?video_format_id={video_format_id}")
        selected_video_family = str(video.get("vcodec") or "custom")
    else:
        video_family = _canonical_video_family(video_selector)
        video = _choose_best_format([fmt for fmt in formats if _matches_video_family(fmt, video_family)], kind="video")
        selected_video_family = video_family

    if audio_format_id:
        audio = _find_format_by_id(info, audio_format_id)
        if not audio or not _is_audio_only(audio):
            raise web.HTTPBadRequest(text=f"unknown or non-audio ?audio_format_id={audio_format_id}")
        selected_audio_family = str(audio.get("acodec") or "custom")
    else:
        audio_family = _canonical_audio_family(audio_selector)
        audio = _choose_best_format([fmt for fmt in formats if _matches_audio_family(fmt, audio_family)], kind="audio")
        selected_audio_family = audio_family

    family = f"{selected_video_family}+{selected_audio_family}"
    return {"family": family, "video": video, "audio": audio}


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
    # subtitles crash vlc for some reason
    return []
    """Extract one vtt subtitle track per language from manually uploaded subtitles.
    Fallback to automatic_captions if no manual subtitles exist."""
    # Prefer manually uploaded subtitles (not auto-generated)
    subtitles = info.get("subtitles") or {}
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
) -> dict[str, Any] | None:
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
    result: dict[str, Any] = {"init_range": init_range, "container": "mp4"}
    if sidx:
        result["index_range"] = f"{sidx['start']}-{sidx['end']}"
        sidx_info = _parse_sidx(body, int(sidx["start"]), int(sidx["size"]))
        if sidx_info:
            result["sidx"] = sidx_info
    return result


def _ebml_width(first_byte: int) -> int:
    mask = 0x80
    width = 1
    while width <= 8 and not (first_byte & mask):
        mask >>= 1
        width += 1
    if width > 8:
        raise ValueError("invalid EBML vint")
    return width


def _read_ebml_id(buf: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(buf):
        raise ValueError("missing EBML id")
    width = _ebml_width(buf[offset])
    end = offset + width
    if end > len(buf):
        raise ValueError("truncated EBML id")
    value = 0
    for b in buf[offset:end]:
        value = (value << 8) | b
    return value, width


def _read_ebml_size(buf: bytes, offset: int) -> tuple[int | None, int]:
    if offset >= len(buf):
        raise ValueError("missing EBML size")
    width = _ebml_width(buf[offset])
    end = offset + width
    if end > len(buf):
        raise ValueError("truncated EBML size")
    value = buf[offset] & ((1 << (8 - width)) - 1)
    for b in buf[offset + 1:end]:
        value = (value << 8) | b
    max_value = (1 << (7 * width)) - 1
    if value == max_value:
        return None, width
    return value, width


def _read_ebml_uint(buf: bytes, start: int, size: int) -> int:
    end = start + size
    if end > len(buf):
        raise ValueError("truncated EBML uint")
    value = 0
    for b in buf[start:end]:
        value = (value << 8) | b
    return value


def _iter_ebml_elements(buf: bytes, start: int, end: int, *, absolute_base: int = 0):
    pos = start
    limit = min(end, len(buf))
    while pos < limit:
        try:
            elem_id, id_width = _read_ebml_id(buf, pos)
            size, size_width = _read_ebml_size(buf, pos + id_width)
        except ValueError:
            break
        data_start = pos + id_width + size_width
        truncated = False
        if size is None:
            data_end = limit
            next_pos = limit
            truncated = True
        else:
            full_data_end = data_start + size
            if full_data_end > limit:
                data_end = limit
                next_pos = limit
                truncated = True
            else:
                data_end = full_data_end
                next_pos = full_data_end
        yield {
            "id": elem_id,
            "header_start": absolute_base + pos,
            "data_start": absolute_base + data_start,
            "data_end": absolute_base + data_end,
            "size": None if size is None else int(size),
            "local_data_start": data_start,
            "local_data_end": data_end,
            "truncated": truncated,
        }
        pos = next_pos


def _parse_webm_init_metadata(buf: bytes, absolute_base: int = 0) -> dict[str, int] | None:
    segment = next((elem for elem in _iter_ebml_elements(buf, 0, len(buf), absolute_base=absolute_base) if elem["id"] == 0x18538067), None)
    if not segment:
        return None

    segment_data_start = int(segment["data_start"])
    segment_local_start = int(segment["local_data_start"])
    segment_local_end = int(segment["local_data_end"])
    first_cluster_start: int | None = None
    timecode_scale = 1_000_000
    duration_ns: int | None = None

    for elem in _iter_ebml_elements(buf, segment_local_start, segment_local_end, absolute_base=absolute_base):
        elem_id = int(elem["id"])
        if elem_id == 0x1549A966:
            for info_elem in _iter_ebml_elements(buf, int(elem["local_data_start"]), int(elem["local_data_end"]), absolute_base=absolute_base):
                if int(info_elem["id"]) == 0x2AD7B1:
                    timecode_scale = _read_ebml_uint(buf, int(info_elem["local_data_start"]), int(info_elem["size"] or 0))
                elif int(info_elem["id"]) == 0x4489:
                    raw = buf[int(info_elem["local_data_start"]):int(info_elem["local_data_end"])]
                    if len(raw) == 4:
                        duration_ns = int(struct.unpack('>f', raw)[0] * timecode_scale)
                    elif len(raw) == 8:
                        duration_ns = int(struct.unpack('>d', raw)[0] * timecode_scale)
        elif elem_id == 0x1F43B675 and first_cluster_start is None:
            first_cluster_start = int(elem["header_start"])
            break

    if first_cluster_start is None:
        return None

    result = {
        "segment_data_start": segment_data_start,
        "first_cluster_start": first_cluster_start,
        "timecode_scale": timecode_scale,
    }
    if duration_ns is not None:
        result["duration_ns"] = duration_ns
    return result


def _parse_webm_cues(buf: bytes, absolute_base: int = 0) -> list[dict[str, int]]:
    cue_points: list[dict[str, int]] = []
    cues_elements = []

    segment = next((elem for elem in _iter_ebml_elements(buf, 0, len(buf), absolute_base=absolute_base) if elem["id"] == 0x18538067), None)
    if segment:
        cues_elements.extend(
            elem
            for elem in _iter_ebml_elements(buf, int(segment["local_data_start"]), int(segment["local_data_end"]), absolute_base=absolute_base)
            if int(elem["id"]) == 0x1C53BB6B
        )
    else:
        cues_elements.extend(
            elem
            for elem in _iter_ebml_elements(buf, 0, len(buf), absolute_base=absolute_base)
            if int(elem["id"]) == 0x1C53BB6B
        )

    for elem in cues_elements:
        for cue_point in _iter_ebml_elements(buf, int(elem["local_data_start"]), int(elem["local_data_end"]), absolute_base=absolute_base):
            if int(cue_point["id"]) != 0xBB:
                continue
            cue_time: int | None = None
            cluster_position: int | None = None
            for cue_elem in _iter_ebml_elements(buf, int(cue_point["local_data_start"]), int(cue_point["local_data_end"]), absolute_base=absolute_base):
                cue_elem_id = int(cue_elem["id"])
                if cue_elem_id == 0xB3:
                    cue_time = _read_ebml_uint(buf, int(cue_elem["local_data_start"]), int(cue_elem["size"] or 0))
                elif cue_elem_id == 0xB7:
                    current_track: int | None = None
                    current_cluster_position: int | None = None
                    for pos_elem in _iter_ebml_elements(buf, int(cue_elem["local_data_start"]), int(cue_elem["local_data_end"]), absolute_base=absolute_base):
                        pos_elem_id = int(pos_elem["id"])
                        if pos_elem_id == 0xF7:
                            current_track = _read_ebml_uint(buf, int(pos_elem["local_data_start"]), int(pos_elem["size"] or 0))
                        elif pos_elem_id == 0xF1:
                            current_cluster_position = _read_ebml_uint(buf, int(pos_elem["local_data_start"]), int(pos_elem["size"] or 0))
                    if current_cluster_position is not None and (current_track in (None, 1)):
                        cluster_position = current_cluster_position
            if cue_time is not None and cluster_position is not None:
                cue_points.append({"time": cue_time, "cluster_position": cluster_position})
    cue_points.sort(key=lambda item: item["cluster_position"])
    return cue_points


def _build_webm_probe(init_meta: dict[str, int], cue_points: list[dict[str, int]], content_length: int | None) -> dict[str, Any] | None:
    if not cue_points or content_length is None:
        return None

    segment_data_start = init_meta["segment_data_start"]
    first_cluster_start = init_meta["first_cluster_start"]
    timecode_scale = init_meta["timecode_scale"]
    duration_ns = init_meta.get("duration_ns")

    cluster_starts = [segment_data_start + item["cluster_position"] for item in cue_points]
    segments: list[dict[str, int]] = []
    for idx, start in enumerate(cluster_starts):
        end = cluster_starts[idx + 1] - 1 if idx < len(cluster_starts) - 1 else content_length - 1
        segment_info = {
            "range_start": start,
            "range_end": end,
            "time": cue_points[idx]["time"],
        }
        if idx < len(cue_points) - 1:
            segment_info["duration"] = cue_points[idx + 1]["time"] - cue_points[idx]["time"]
        elif duration_ns is not None:
            duration_in_scale = max(0, int(round(duration_ns / timecode_scale)) - cue_points[idx]["time"])
            if duration_in_scale > 0:
                segment_info["duration"] = duration_in_scale
        segments.append(segment_info)

    return {
        "container": "webm",
        "timescale": 1000,
        "timecode_scale": timecode_scale,
        "duration_ns": duration_ns,
        "init_range": f"0-{first_cluster_start - 1}",
        "segments": segments,
    }


async def _fetch_content_length(session: aiohttp.ClientSession, url: str) -> int | None:
    proxy = _get_proxy()
    async with session.head(url, proxy=proxy) as resp:
        content_length = resp.headers.get("Content-Length")
        log.info(
            "webm probe HEAD url=%s final_url=%s status=%s content_length=%s content_range=%s",
            url,
            resp.url,
            resp.status,
            content_length,
            resp.headers.get("Content-Range"),
        )
        if resp.status >= 400:
            return None
        return int(content_length) if content_length and content_length.isdigit() else None


async def _fetch_webm_probe(
    session: aiohttp.ClientSession,
    url: str,
    *,
    front_probe_bytes: int = 2 * 1024 * 1024,
    tail_probe_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any] | None:
    proxy = _get_proxy()
    content_length = await _fetch_content_length(session, url)
    log.info(
        "webm probe start url=%s content_length=%s front_probe_bytes=%s tail_probe_bytes=%s",
        url,
        content_length,
        front_probe_bytes,
        tail_probe_bytes,
    )

    async with session.get(url, proxy=proxy, headers={"Range": f"bytes=0-{front_probe_bytes - 1}"}) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(text or f"webm probe request failed: {resp.status}")
        front = await resp.read()
        log.info(
            "webm probe front GET url=%s final_url=%s status=%s content_length=%s content_range=%s first32=%s",
            url,
            resp.url,
            resp.status,
            resp.headers.get("Content-Length"),
            resp.headers.get("Content-Range"),
            front[:32].hex(),
        )
    log.info("webm probe front bytes=%s url=%s", len(front), url)

    init_meta = _parse_webm_init_metadata(front, 0)
    if not init_meta:
        log.warning("webm probe missing init metadata from front chunk url=%s front_bytes=%s", url, len(front))
        return None
    log.info("webm probe init metadata url=%s init_meta=%s", url, init_meta)

    cue_points = _parse_webm_cues(front, 0)
    log.info("webm probe front cues url=%s cue_count=%s", url, len(cue_points))
    if cue_points:
        probe = _build_webm_probe(init_meta, cue_points, content_length)
        if probe:
            log.info("webm probe built from front cues url=%s segment_count=%s", url, len(probe.get("segments") or []))
            return probe
        log.warning("webm probe front cues found but probe build returned none url=%s content_length=%s", url, content_length)

    if content_length and content_length > tail_probe_bytes:
        start = max(0, content_length - tail_probe_bytes)
        async with session.get(url, proxy=proxy, headers={"Range": f"bytes={start}-{content_length - 1}"}) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(text or f"webm tail probe request failed: {resp.status}")
            tail = await resp.read()
            log.info(
                "webm probe tail GET url=%s final_url=%s status=%s content_length=%s content_range=%s first32=%s",
                url,
                resp.url,
                resp.status,
                resp.headers.get("Content-Length"),
                resp.headers.get("Content-Range"),
                tail[:32].hex(),
            )
        log.info("webm probe tail bytes=%s start=%s url=%s", len(tail), start, url)
        cue_points = _parse_webm_cues(tail, start)
        log.info("webm probe tail cues url=%s cue_count=%s", url, len(cue_points))
        probe = _build_webm_probe(init_meta, cue_points, content_length)
        if probe:
            log.info("webm probe built from tail cues url=%s segment_count=%s", url, len(probe.get("segments") or []))
            return probe
        log.warning(
            "webm probe tail path returned none url=%s content_length=%s tail_start=%s cue_count=%s",
            url,
            content_length,
            start,
            len(cue_points),
        )
    else:
        log.warning(
            "webm probe skipped tail fetch url=%s content_length=%s tail_probe_bytes=%s",
            url,
            content_length,
            tail_probe_bytes,
        )

    log.warning("webm probe returning none url=%s", url)
    return None


def _representation_to_dom(
    doc: Document,
    adaptation_set_node: Element,
    fmt: dict[str, Any],
    *,
    audio_lang: str | None = None,
    segment_probe: dict[str, Any] | None = None,
) -> None:
    codec = str(fmt.get("vcodec") if _is_video_only(fmt) else fmt.get("acodec"))
    bandwidth = int(_bandwidth(fmt))

    repr_node = doc.createElement("Representation")
    repr_node.setAttribute("id", str(fmt["format_id"]))
    repr_node.setAttribute("mimeType", _mime_type(fmt))
    repr_node.setAttribute("codecs", codec)
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

    webm_segments = segment_probe.get("segments") if segment_probe and segment_probe.get("container") == "webm" else None
    sidx_info = segment_probe.get("sidx") if segment_probe else None
    if webm_segments:
        segment_list_node = doc.createElement("SegmentList")
        if segment_probe and segment_probe.get("timescale"):
            segment_list_node.setAttribute("timescale", str(segment_probe["timescale"]))
        if segment_probe and segment_probe.get("init_range"):
            init_node = doc.createElement("Initialization")
            init_node.setAttribute("range", segment_probe["init_range"])
            segment_list_node.appendChild(init_node)
        for seg in webm_segments:
            seg_url_node = doc.createElement("SegmentURL")
            seg_url_node.setAttribute("mediaRange", f"{seg['range_start']}-{seg['range_end']}")
            segment_list_node.appendChild(seg_url_node)
        repr_node.appendChild(segment_list_node)
    elif sidx_info and sidx_info.get("segments"):
        segment_list_node = doc.createElement("SegmentList")
        segment_list_node.setAttribute("timescale", str(sidx_info["timescale"]))
        if segment_probe and segment_probe.get("duration"):
            duration_seconds = float(segment_probe["duration"])
            total_ticks = max(1, int(round(duration_seconds * int(sidx_info["timescale"]))))
            avg_ticks = max(1, int(round(total_ticks / len(sidx_info["segments"]))))
            segment_list_node.setAttribute("duration", str(avg_ticks))
        if segment_probe and segment_probe.get("init_range"):
            init_node = doc.createElement("Initialization")
            init_node.setAttribute("range", segment_probe["init_range"])
            segment_list_node.appendChild(init_node)

        for seg in sidx_info["segments"]:
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
    selected = _choose_av_pair(
        info,
        video_selector=request.query.get("video"),
        audio_selector=request.query.get("audio"),
        video_format_id=request.query.get("video_format_id"),
        audio_format_id=request.query.get("audio_format_id"),
    )
    probes = await _probe_selected_formats(selected, video_id)
    subtitles = _pick_vtt_subtitles(info)
    available = _list_selectable_formats(info)
    query = dict(request.query)
    return web.json_response(
        {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "video_id": info.get("id"),
            "selected_family": selected["family"],
            "selected_query": {
                "video": query.get("video", "avc"),
                "audio": query.get("audio", "aac"),
                "video_format_id": query.get("video_format_id"),
                "audio_format_id": query.get("audio_format_id"),
            },
            "video": {
                key: selected["video"].get(key)
                for key in ("format_id", "ext", "vcodec", "width", "height", "fps", "tbr", "filesize", "url")
            },
            "audio": {
                key: selected["audio"].get(key)
                for key in ("format_id", "ext", "acodec", "asr", "audio_channels", "tbr", "filesize", "language", "url")
            },
            "available_formats": available,
            "query_params": {
                "video": ["avc", "vp9", "av1"],
                "audio": ["aac", "opus"],
                "video_format_id": "exact yt-dlp video-only format_id override",
                "audio_format_id": "exact yt-dlp audio-only format_id override",
            },
            "subtitles": subtitles,
            "probes": probes,
            "manifest_url": str(request.url.with_path("/manifest").with_query(request.query)),
        }
    )


async def manifest_handler(request: web.Request) -> web.Response:
    log.info("GET /manifest %s %s %s", request.query.get("url"), request.query.get("video"),request.query.get("audio"))
    try:
        info, video_id = await _resolve_info(request)
        selected = _choose_av_pair(
            info,
            video_selector=request.query.get("video"),
            audio_selector=request.query.get("audio"),
            video_format_id=request.query.get("video_format_id"),
            audio_format_id=request.query.get("audio_format_id"),
        )
        probes = await _probe_selected_formats(selected, video_id)
        mpd = build_mpd(info, selected, probes)
    except web.HTTPException:
        raise
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

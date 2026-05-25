#!/usr/bin/env python3
"""Minimal YouTube DASH manifest proxy POC for VLC."""

import asyncio
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import aiohttp
from aiohttp import web
import yt_dlp

import logging


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ytmpd-poc")


def _get_proxy() -> str | None:
    return os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")


def _manifest_kind(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.lower()
    if path.endswith(".mpd") or "/dash/" in path:
        return "dash"
    if path.endswith(".m3u8") or "/hls_" in path:
        return "hls"
    return None


def _proxy_asset_url(request: web.Request, target_url: str) -> str:
    return str(
        request.url.with_path("/hls/asset").with_query({"target": target_url})
    )


def _proxy_dash_asset_url(request: web.Request, target_url: str) -> str:
    return str(
        request.url.with_path("/dash/asset").with_query({"target": target_url})
    )


def _rewrite_hls_playlist(body: str, base_url: str, request: web.Request) -> str:
    rewritten_lines = []
    uri_pattern = re.compile(r'URI="([^"]+)"')

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            rewritten_lines.append(raw_line)
            continue

        if line.startswith("#"):
            def _replace_uri(match: re.Match[str]) -> str:
                absolute = urljoin(base_url, match.group(1))
                return f'URI="{_proxy_asset_url(request, absolute)}"'

            rewritten_lines.append(uri_pattern.sub(_replace_uri, raw_line))
            continue

        absolute = urljoin(base_url, line)
        rewritten_lines.append(_proxy_asset_url(request, absolute))

    return "\n".join(rewritten_lines) + "\n"


def _select_hls_variant_playlist(master_body: str, base_url: str) -> str | None:
    best_url = None
    best_score = -1
    pending_score = None

    for raw_line in master_body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.partition(":")[2]
            score = 0

            bandwidth_match = re.search(r"BANDWIDTH=(\d+)", attrs)
            if bandwidth_match:
                score += int(bandwidth_match.group(1))

            resolution_match = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
            if resolution_match:
                score += int(resolution_match.group(1)) * int(resolution_match.group(2))

            pending_score = score
            continue

        if line.startswith("#"):
            continue

        if pending_score is None:
            continue

        if pending_score > best_score:
            best_score = pending_score
            best_url = urljoin(base_url, line)
        pending_score = None

    return best_url


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rewrite_mpd(body: bytes, base_url: str, request: web.Request) -> bytes:
    """Rewrite DASH asset references so VLC fetches all media through this server."""
    root = ET.fromstring(body)

    def walk(element: ET.Element, inherited_base_url: str) -> None:
        current_base_url = inherited_base_url

        for child in list(element):
            if _local_name(child.tag) != "BaseURL":
                continue

            text = (child.text or "").strip()
            if not text:
                continue

            absolute = urljoin(inherited_base_url, text)
            child.text = _proxy_dash_asset_url(request, absolute)
            current_base_url = absolute

        for attr_name in ("media", "initialization", "sourceURL", "href"):
            value = element.attrib.get(attr_name)
            if not value:
                continue
            if value.startswith(("data:", "urn:")):
                continue
            absolute = urljoin(current_base_url, value)
            element.set(attr_name, _proxy_dash_asset_url(request, absolute))

        for child in list(element):
            if _local_name(child.tag) == "BaseURL":
                continue
            walk(child, current_base_url)

    walk(root, base_url)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _extract_manifest_info(youtube_url: str) -> dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "allow_duplicate": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    manifests: dict[str, str] = {}
    manifest_candidates = []

    top_manifest_url = info.get("manifest_url")
    top_kind = _manifest_kind(top_manifest_url)
    if top_manifest_url and top_kind:
        manifests[top_kind] = top_manifest_url

    for fmt in info.get("formats", []):
        candidate = fmt.get("manifest_url")
        kind = _manifest_kind(candidate)
        if not candidate or not kind:
            continue
        manifests.setdefault(kind, candidate)
        manifest_candidates.append(
            {
                "format_id": fmt.get("format_id"),
                "kind": kind,
                "protocol": fmt.get("protocol"),
                "ext": fmt.get("ext"),
                "acodec": fmt.get("acodec"),
                "vcodec": fmt.get("vcodec"),
                "manifest_url": candidate,
            }
        )

    if not manifests:
        raise RuntimeError("No HLS or DASH manifest_url found for this video")

    manifest_kind = "dash" if "dash" in manifests else "hls"
    manifest_url = manifests[manifest_kind]

    return {
        "title": info.get("title") or "untitled",
        "video_id": info.get("id"),
        "webpage_url": info.get("webpage_url") or youtube_url,
        "manifest_kind": manifest_kind,
        "manifest_url": manifest_url,
        "manifests": manifests,
        "manifest_candidates": manifest_candidates,
        "duration": info.get("duration"),
        "live_status": info.get("live_status"),
    }


async def info_handler(request: web.Request) -> web.Response:
    youtube_url = request.query.get("url")
    log.info("GET /info url=%s", youtube_url)
    if not youtube_url:
        return web.json_response({"error": "missing ?url=<youtube_url>"}, status=400)

    try:
        info = await asyncio.to_thread(_extract_manifest_info, youtube_url)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)

    play_url = str(
        request.url.with_path("/play").with_query({"url": youtube_url})
    )
    manifest_proxy_url = str(
        request.url.with_path("/manifest").with_query(
            {"url": youtube_url, "kind": info["manifest_kind"]}
        )
    )
    hls_proxy_url = str(
        request.url.with_path("/hls/master").with_query({"url": youtube_url})
    )
    info["play_url"] = play_url
    info["manifest_proxy_url"] = manifest_proxy_url
    info["hls_proxy_url"] = hls_proxy_url
    return web.json_response(info)


async def play_handler(request: web.Request) -> web.Response:
    youtube_url = request.query.get("url")
    kind = request.query.get("kind")
    log.info("GET /play url=%s kind=%s", youtube_url, kind)
    if not youtube_url:
        return web.Response(text="missing ?url=<youtube_url>", status=400)

    try:
        info = await asyncio.to_thread(_extract_manifest_info, youtube_url)
    except Exception as exc:
        return web.Response(text=str(exc), status=400)

    kind = kind or info["manifest_kind"]
    if kind == "hls":
        manifest_url = info["manifests"].get("hls")
        if not manifest_url:
            return web.Response(text="No HLS manifest found for this video", status=404)

        proxy = _get_proxy()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            log.info("fetch master for variant selection upstream=%s", manifest_url)
            async with session.get(manifest_url, proxy=proxy) as upstream:
                master_body = await upstream.text()
                variant_url = _select_hls_variant_playlist(master_body, str(upstream.url))

        if variant_url:
            target = _proxy_asset_url(request, variant_url)
            log.info("redirecting VLC to selected HLS variant: %s", target)
            raise web.HTTPFound(target)

        fallback = str(request.url.with_path("/hls/master").with_query({"url": youtube_url}))
        log.info("no HLS variant selected, falling back to master proxy: %s", fallback)
        raise web.HTTPFound(fallback)

    manifest_url = info["manifests"].get(kind)
    if not manifest_url:
        return web.Response(text=f"manifest kind not found: {kind}", status=404)
    if kind == "dash":
        target = str(request.url.with_path("/manifest").with_query({"url": youtube_url, "kind": "dash"}))
        log.info("redirecting VLC to local DASH proxy: %s", target)
        raise web.HTTPFound(target)
    log.info("redirecting VLC to upstream manifest kind=%s", kind)
    raise web.HTTPFound(manifest_url)


async def manifest_handler(request: web.Request) -> web.StreamResponse:
    youtube_url = request.query.get("url")
    kind = request.query.get("kind")
    log.info("GET /manifest url=%s kind=%s", youtube_url, kind)
    if not youtube_url:
        return web.Response(text="missing ?url=<youtube_url>", status=400)

    try:
        info = await asyncio.to_thread(_extract_manifest_info, youtube_url)
    except Exception as exc:
        return web.Response(text=str(exc), status=400)

    kind = kind or info["manifest_kind"]
    manifest_url = info["manifests"].get(kind)
    if not manifest_url:
        return web.Response(text=f"manifest kind not found: {kind}", status=404)

    proxy = _get_proxy()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": (
            "application/dash+xml,application/vnd.apple.mpegurl,"
            "application/x-mpegURL,application/xml,text/xml;q=0.9,*/*;q=0.1"
        ),
    }
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        log.info("proxy manifest kind=%s upstream=%s", kind, manifest_url)
        async with session.get(manifest_url, proxy=proxy) as upstream:
            if upstream.status >= 400:
                text = await upstream.text()
                return web.Response(
                    text=text or f"upstream manifest error: {upstream.status}",
                    status=upstream.status,
                )

            body = await upstream.read()
            if kind == "dash":
                body = _rewrite_mpd(body, str(upstream.url), request)
            resp = web.Response(
                body=body,
                content_type=(
                    "application/dash+xml"
                    if kind == "dash"
                    else "application/vnd.apple.mpegurl"
                ),
                headers={
                    "X-YouTube-Title": info["title"],
                    "X-YouTube-ID": info["video_id"] or "",
                    "X-Manifest-Kind": kind,
                },
            )
            return resp


async def dash_asset_handler(request: web.Request) -> web.StreamResponse:
    target = request.query.get("target")
    range_header = request.headers.get("Range")
    log.info("GET /dash/asset target=%s range=%s", target, range_header)
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
        log.info("fetch dash asset upstream=%s", target)
        async with session.get(target, proxy=proxy) as upstream:
            if upstream.status >= 400:
                text = await upstream.text()
                return web.Response(
                    text=text or f"upstream asset error: {upstream.status}",
                    status=upstream.status,
                )

            resp = web.StreamResponse(status=upstream.status)
            passthrough_headers = (
                "Content-Type",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
                "ETag",
                "Last-Modified",
            )
            for name in passthrough_headers:
                value = upstream.headers.get(name)
                if value:
                    resp.headers[name] = value
            resp.headers["Cache-Control"] = "no-store"
            await resp.prepare(request)

            total = 0
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                total += len(chunk)
                await resp.write(chunk)
            await resp.write_eof()
            log.info(
                "streamed dash asset upstream=%s status=%s bytes=%d",
                upstream.url,
                upstream.status,
                total,
            )
            return resp


async def hls_master_handler(request: web.Request) -> web.StreamResponse:
    youtube_url = request.query.get("url")
    log.info("GET /hls/master url=%s", youtube_url)
    if not youtube_url:
        return web.Response(text="missing ?url=<youtube_url>", status=400)

    try:
        info = await asyncio.to_thread(_extract_manifest_info, youtube_url)
    except Exception as exc:
        return web.Response(text=str(exc), status=400)

    manifest_url = info["manifests"].get("hls")
    if not manifest_url:
        return web.Response(text="No HLS manifest found for this video", status=404)

    return await _serve_hls_playlist(request, manifest_url, info)


async def _serve_hls_playlist(
    request: web.Request, playlist_url: str, info: dict[str, Any] | None = None
) -> web.StreamResponse:
    proxy = _get_proxy()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*",
    }
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        log.info("fetch playlist upstream=%s", playlist_url)
        async with session.get(playlist_url, proxy=proxy) as upstream:
            if upstream.status >= 400:
                text = await upstream.text()
                return web.Response(
                    text=text or f"upstream playlist error: {upstream.status}",
                    status=upstream.status,
                )

            body = await upstream.text()
            rewritten = _rewrite_hls_playlist(body, str(upstream.url), request)
            log.info("rewrote playlist upstream=%s bytes=%d", upstream.url, len(rewritten))
            resp = web.Response(
                text=rewritten,
                content_type="application/vnd.apple.mpegurl",
            )
            if info:
                resp.headers["X-YouTube-Title"] = info["title"]
                resp.headers["X-YouTube-ID"] = info["video_id"] or ""
            resp.headers["Cache-Control"] = "no-store"
            return resp


async def hls_asset_handler(request: web.Request) -> web.StreamResponse:
    target = request.query.get("target")
    range_header = request.headers.get("Range")
    log.info("GET /hls/asset target=%s range=%s", target, range_header)
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
        log.info("fetch asset upstream=%s", target)
        async with session.get(target, proxy=proxy) as upstream:
            if upstream.status >= 400:
                text = await upstream.text()
                return web.Response(
                    text=text or f"upstream asset error: {upstream.status}",
                    status=upstream.status,
                )

            content_type = upstream.headers.get("Content-Type", "")
            if "mpegurl" in content_type.lower() or target.endswith(".m3u8"):
                body = await upstream.text()
                rewritten = _rewrite_hls_playlist(body, str(upstream.url), request)
                log.info("rewrote nested playlist upstream=%s bytes=%d", upstream.url, len(rewritten))
                resp = web.Response(
                    text=rewritten,
                    status=upstream.status,
                    content_type="application/vnd.apple.mpegurl",
                )
            else:
                resp = web.StreamResponse(status=upstream.status)
                passthrough_headers = (
                    "Content-Type",
                    "Content-Length",
                    "Content-Range",
                    "Accept-Ranges",
                    "ETag",
                    "Last-Modified",
                )
                for name in passthrough_headers:
                    value = upstream.headers.get(name)
                    if value:
                        resp.headers[name] = value
                await resp.prepare(request)
                total = 0
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
                log.info(
                    "streamed asset upstream=%s status=%s bytes=%d",
                    upstream.url,
                    upstream.status,
                    total,
                )
                return resp

            resp.headers["Cache-Control"] = "no-store"
            return resp


def main() -> None:
    app = web.Application()
    app.router.add_get("/info", info_handler)
    app.router.add_get("/play", play_handler)
    app.router.add_get("/manifest", manifest_handler)
    app.router.add_get("/dash/asset", dash_asset_handler)
    app.router.add_get("/hls/master", hls_master_handler)
    app.router.add_get("/hls/asset", hls_asset_handler)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8288"))
    log.info("starting DASH/HLS proxy POC on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""YouTube URL transcoder proxy: yt-dlp library + PyAV streaming mux."""

import asyncio
import fcntl
import logging
import os
import threading

import yt_dlp
from aiohttp import web
import aiohttp
import av
from av.codec.codec import UnknownCodecError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ytmux")


def _get_proxy() -> str | None:
    """Read proxy from environment, matching yt-dlp convention."""
    return os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")

# ── defaults ─────────────────────────────────────────────────────────────────

DEFAULT_FORMAT = "webm"
EMIT_CHUNK_SIZE = 256 * 1024
# For webm, pyav doesn't need special flags. For mp4:
# options={'movflags': 'frag_keyframe+empty_moov+default_base_moof'}


def _safe_setattr(obj, name, value) -> None:
    if value is None:
        return
    try:
        setattr(obj, name, value)
    except (AttributeError, ValueError, TypeError):
        pass


def _safe_getattr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except (AttributeError, RuntimeError, ValueError, TypeError):
        return default


def _resolve_mux_codec_name(in_stream) -> str:
    """Map decoder-specific names like libdav1d/libopus to muxable codec names."""
    codec_names = []

    for candidate in (
        getattr(in_stream.codec_context, "name", None),
        getattr(getattr(in_stream, "codec", None), "name", None),
    ):
        if candidate and candidate not in codec_names:
            codec_names.append(candidate)

    for candidate in codec_names:
        try:
            return av.Codec(candidate, "r").canonical_name
        except UnknownCodecError:
            continue

    raise UnknownCodecError(codec_names[0] if codec_names else "<unknown>")


def _add_remux_stream(output, in_stream):
    """Create an output stream compatible with the current PyAV/FFmpeg build."""
    try:
        return output.add_stream_from_template(in_stream)
    except UnknownCodecError as e:
        codec_name = _resolve_mux_codec_name(in_stream)
        log.warning(
            "template remux failed for %s stream codec=%s decoder=%s canonical=%s: %s; falling back",
            in_stream.type,
            getattr(in_stream.codec_context, "name", None),
            in_stream.codec.name,
            codec_name,
            e,
        )

        if in_stream.type == "video":
            out_stream = output.add_stream(codec_name, rate=in_stream.average_rate)
            _safe_setattr(out_stream, "time_base", in_stream.time_base)
            _safe_setattr(out_stream.codec_context, "extradata", _safe_getattr(in_stream.codec_context, "extradata"))
            _safe_setattr(out_stream.codec_context, "width", _safe_getattr(in_stream.codec_context, "width"))
            _safe_setattr(out_stream.codec_context, "height", _safe_getattr(in_stream.codec_context, "height"))
            _safe_setattr(out_stream.codec_context, "pix_fmt", _safe_getattr(in_stream.codec_context, "pix_fmt"))
            return out_stream

        if in_stream.type == "audio":
            out_stream = output.add_stream(codec_name, rate=_safe_getattr(in_stream.codec_context, "sample_rate"))
            _safe_setattr(out_stream, "time_base", in_stream.time_base)
            _safe_setattr(out_stream.codec_context, "extradata", _safe_getattr(in_stream.codec_context, "extradata"))
            _safe_setattr(out_stream.codec_context, "sample_rate", _safe_getattr(in_stream.codec_context, "sample_rate"))
            _safe_setattr(out_stream.codec_context, "channels", _safe_getattr(in_stream.codec_context, "channels"))
            _safe_setattr(out_stream.codec_context, "layout", _safe_getattr(in_stream.codec_context, "layout"))
            return out_stream

        raise


class StreamBuffer:
    """Thread-safe FIFO buffer: PyAV writes into it, we drain on each mux."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self._buf.extend(data)
        return len(data)

    def flush(self):
        pass

    def get_and_clear(self) -> bytes:
        with self._lock:
            result = bytes(self._buf)
            self._buf.clear()
            return result


# ── yt-dlp library interface ─────────────────────────────────────────────────

def get_urls(youtube_url: str) -> dict[str, str]:
    """Return {"video": url, "audio": url} for best quality video+audio."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "allow_duplicate": True,
        "extract_flat": False,
        # Prefer WebM-native VP9/Opus to keep remuxing simple and avoid AV1
        # decoder/template issues in PyAV.
        "format": (
            "bestvideo[ext=webm][vcodec!*=av01]+bestaudio[ext=webm][acodec*=opus]"
            "/bestvideo[vcodec!*=av01]+bestaudio"
            "/best[vcodec!*=av01]"
            "/best"
        ),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

        if info.get("requested_formats") is None:
            raise RuntimeError("No requested formats found")

        video_format, audio_format = info["requested_formats"]

        video_url = video_format["url"]
        audio_url = audio_format["url"]

        # If manifest_url is set, yt-dlp returned a playlist URL.
        # Re-resolve via extract_info to get actual media URLs.
        if "manifest_url" in video_format:
            video_url = _resolve_manifest(ydl, video_url)
        if "manifest_url" in audio_format:
            audio_url = _resolve_manifest(ydl, audio_url)

        return {"video": video_url, "audio": audio_url}


def _resolve_manifest(ydl: yt_dlp.YoutubeDL, manifest_url: str) -> str:
    """Resolve a DASH/HLS manifest URL to the actual media URL."""
    try:
        info = ydl.extract_info(manifest_url, download=False)
        formats = info.get("formats", [])
        if formats:
            return formats[0]["url"]
    except Exception:
        pass
    return manifest_url


# ── streaming muxer (aiohttp -> pipe -> PyAV) ────────────────────────────────

async def _stream_to_pipe(url: str, write_fd: int, stop_event: threading.Event,
                          max_retries: int = 5) -> None:
    """Stream a URL to a pipe using aiohttp, with retry on failure."""
    for attempt in range(1, max_retries + 1):
        if stop_event.is_set():
            return
        try:
            proxy = _get_proxy()
            async with aiohttp.ClientSession(
                proxy=proxy,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as session:
                async with session.get(url) as resp:
                    async for chunk in resp.content.iter_chunked(65536):
                        if stop_event.is_set():
                            return
                        os.write(write_fd, chunk)
            return  # success
        except Exception as e:
            if stop_event.is_set():
                return
            if attempt == max_retries:
                raise
            log.warning("stream attempt %d/%d failed for %s: %s",
                        attempt, max_retries, url, e)
            await asyncio.sleep(min(2 ** attempt, 10))


def generate_muxed_stream(video_url: str, audio_url: str,
                          output_format: str = DEFAULT_FORMAT,
                          stop_event: threading.Event | None = None):
    """Generator: yield muxed webm bytes in real-time.

    Architecture:
      aiohttp (async)  ──write──> pipe ──read──> PyAV demux ──mux──> StreamBuffer
      ──────────────────────────────────────────────────────────────────────────
      background threads                    background thread (sync)

    - aiohttp handles HTTP Range requests (YouTube signed URLs)
    - pipe bridges async HTTP to sync PyAV
    - PyAV does remux (no re-encoding)
    - StreamBuffer drains each yield → zero memory accumulation
    """
    buf = StreamBuffer()
    pending = bytearray()

    # Create pipes: one for video, one for audio
    # Increase pipe buffer size (default 64KB on Linux) to avoid deadlocks
    PIPE_BUF = 64 * 1024 * 1024  # 64MB
    v_r, v_w = os.pipe()
    a_r, a_w = os.pipe()
    for fd in (v_r, v_w, a_r, a_w):
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_BUF)
        except OSError:
            pass  # might fail on non-Linux or old kernels

    stop_event = stop_event or threading.Event()

    def stream_to_pipe(url, write_fd):
        try:
            asyncio.run(_stream_to_pipe(url, write_fd, stop_event, max_retries=5))
        except Exception as e:
            log.error("stream error: %s", e)
            stop_event.set()
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass

    # Start streaming threads
    t1 = threading.Thread(target=stream_to_pipe, args=(video_url, v_w))
    t2 = threading.Thread(target=stream_to_pipe, args=(audio_url, a_w))
    t1.start()
    t2.start()

    # Wait for data to flow into pipes before opening with PyAV.
    # Without this, av.open() blocks forever on empty pipes.
    def _wait_for_pipe(fd, name, timeout=30):
        import select
        deadline = threading.Event()
        def _set():
            import time; time.sleep(timeout)
            deadline.set()
        threading.Thread(target=_set, daemon=True).start()
        while not deadline.is_set() and not stop_event.is_set():
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                return
        if stop_event.is_set():
            raise RuntimeError(f"Stopped while waiting for {name} pipe")
        raise RuntimeError(f"No data received on {name} pipe after {timeout}s")

    try:
        _wait_for_pipe(v_r, "video", timeout=30)
        _wait_for_pipe(a_r, "audio", timeout=30)
    except Exception as e:
        log.error("pipe wait failed: %s", e)
        t1.join(timeout=5)
        t2.join(timeout=5)
        raise

    frame_count = 0
    try:
        # PyAV reads from pipes (local file descriptors, no HTTP)
        v_stream = av.open(os.fdopen(v_r, 'rb'))
        a_stream = av.open(os.fdopen(a_r, 'rb'))

        video_stream = v_stream.streams.video[0]
        audio_stream = a_stream.streams.audio[0]

        log.info("video: %s %dx%d fps=%.2f", video_stream.codec.name,
                 video_stream.width, video_stream.height,
                 video_stream.average_rate or 0)
        log.info("audio: %s %dHz %dch", audio_stream.codec.name,
                 audio_stream.sample_rate, audio_stream.channels)

        if _resolve_mux_codec_name(video_stream) == "av1":
            raise RuntimeError(
                "Selected AV1 video stream is not remuxable with this PyAV build; "
                "please choose a non-AV1 source format."
            )

        # Create output container
        output = av.open(buf, mode="w", format=output_format)
        out_video = _add_remux_stream(output, video_stream)
        out_audio = _add_remux_stream(output, audio_stream)

        # Create demuxers
        v_demux = v_stream.demux(video_stream)
        a_demux = a_stream.demux(audio_stream)

        next_v = next(v_demux, None)
        next_a = next(a_demux, None)

        log.info("mux loop starting, waiting for first packets...")
        while (next_v is not None or next_a is not None) and not stop_event.is_set():
            # Interleave: pick whichever has earlier DTS
            if next_v is not None and next_a is not None:
                v_time = (next_v.dts * video_stream.time_base
                          if next_v.dts is not None else float("inf"))
                a_time = (next_a.dts * audio_stream.time_base
                          if next_a.dts is not None else float("inf"))
                choose_video = v_time <= a_time
            else:
                choose_video = next_v is not None

            if choose_video:
                if next_v.dts is not None:
                    next_v.stream = out_video
                    output.mux(next_v)
                next_v = next(v_demux, None)
            else:
                if next_a.dts is not None:
                    next_a.stream = out_audio
                    output.mux(next_a)
                next_a = next(a_demux, None)

            # Drain buffer → zero memory accumulation
            chunk = buf.get_and_clear()
            if chunk:
                pending.extend(chunk)
                if len(pending) >= EMIT_CHUNK_SIZE:
                    yield bytes(pending)
                    pending.clear()

            frame_count += 1
            if frame_count % 100 == 0:
                log.info("muxed %d frames, pending=%d bytes",
                         frame_count, len(pending))

        # Flush
        if not stop_event.is_set():
            output.close()
            chunk = buf.get_and_clear()
            if chunk:
                pending.extend(chunk)
            if pending:
                yield bytes(pending)

    finally:
        stop_event.set()
        t1.join(timeout=30)
        t2.join(timeout=30)
        log.info("mux done (%d frames)", frame_count)

# ── aiohttp server ───────────────────────────────────────────────────────────

async def stream_handler(request: web.Request) -> web.StreamResponse:
    """GET /?url=<youtube_url> → streaming webm.

    Runs the PyAV generator in a background thread so aiohttp's event loop
    stays responsive for other clients.
    """
    url = request.query.get("url")
    if not url:
        return web.Response(text="?url=<youtube_url>", status=400)

    log.info("streaming %s", url)
    urls = get_urls(url)
    log.info("video url: %s", urls["video"])
    log.info("audio url: %s", urls["audio"])

    resp = web.StreamResponse(
        headers={
            "Content-Type": f"video/{DEFAULT_FORMAT}",
            "Content-Disposition": 'attachment; filename="merged.webm"',
        },
    )
    await resp.prepare(request)

    # Background thread runs the blocking PyAV mux
    loop = asyncio.get_running_loop()
    result_queue = asyncio.Queue()
    error_holder = []
    stop_event = threading.Event()

    def push_result(item):
        loop.call_soon_threadsafe(result_queue.put_nowait, item)

    def run_mux():
        try:
            for chunk in generate_muxed_stream(urls["video"], urls["audio"], stop_event=stop_event):
                push_result(chunk)
        except Exception as e:
            error_holder.append(e)
        finally:
            push_result(None)  # sentinel

    thread = threading.Thread(target=run_mux, daemon=True)
    thread.start()

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(result_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                transport = request.transport
                if transport is None or transport.is_closing():
                    log.info("client disconnected, stopping mux")
                    stop_event.set()
                    break
                continue
            if chunk is None:
                break
            await resp.write(chunk)
    except ConnectionResetError:
        log.warning("client disconnected")
        stop_event.set()
    finally:
        stop_event.set()
        thread.join(timeout=10)
        try:
            await resp.write_eof()
        except ConnectionResetError:
            pass
        if error_holder:
            raise error_holder[0]
    return resp


async def info_handler(request: web.Request) -> web.Response:
    """GET /info?url=<youtube_url> → JSON with video+audio URLs."""
    url = request.query.get("url")
    if not url:
        return web.Response(text="?url=<youtube_url>", status=400)
    try:
        urls = get_urls(url)
    except RuntimeError as e:
        return web.Response(text=str(e), status=400)
    log.info("info: video=%s audio=%s", urls["video"], urls["audio"])
    return web.json_response(urls)


# ── entry point ──────────────────────────────────────────────────────────────

def main():
    app = web.Application()
    app.router.add_get("/", stream_handler)
    app.router.add_get("/info", info_handler)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8188"))
    web.run_app(app, host=host, port=port, access_log=None)


if __name__ == "__main__":
    main()

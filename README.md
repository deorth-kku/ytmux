# ytmux — YouTube URL transcoder proxy

Takes a YouTube URL, returns a new URL on your server that serves merged webm.

## Design: subprocess + pipe, not a Python binding

ffmpeg has no Python binding, and you don't need one. The pattern is:

```python
proc = await asyncio.create_subprocess_exec(
    "ffmpeg", "-i", video_url, "-i", audio_url,
    "-c", "copy", "-f", "webm", "-",
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
# read proc.stdout in a loop → write to HTTP response
```

ffmpeg reads HTTP URLs directly — no download to temp files, no extra CPU for re-encoding (`-c copy`).

## Two variants

| File | Behavior | Memory |
|------|----------|--------|
| `ytmux_server.py` | Loads whole output into memory, sends in one response | High |
| `ytmux_stream.py` | Generator: chunk-by-chunk, zero buffering | Low |

## Quick start

```bash
pip install aiohttp
python ytmux_stream.py &
# GET http://localhost:8080/?url=https://youtube.com/watch?v=xxx
```

## Endpoints

- `GET /?url=<yt_url>` — merged webm stream
- `GET /info?url=<yt_url>` — JSON with video+audio URLs (no muxing)

## M3U

If VLC can play separate video/audio URLs directly on your device, you can skip muxing entirely:

```bash
python ytmux_m3u_poc.py 'https://youtu.be/VIDEO_ID'
```

That writes a local `.m3u` file containing:

```text
#EXTM3U
#EXTINF:-1,<title>
#EXTVLCOPT:input-slave=<audio_url>
<video_url>
```

Then open that `.m3u` file in VLC.

## DASH Proxy

If VLC needs a server-hosted MPD, run:

```bash
python ytmpd_poc.py
```

Then point VLC at:

```text
http://<server>:8288/play?url=<youtube_url>&kind=dash
```

The server fetches YouTube's DASH manifest, rewrites media URLs to `/dash/asset`, and proxies segment requests back upstream.

## Synthesized MPD

If you want to build a static MPD from yt-dlp info instead of forwarding YouTube's manifest:

```bash
python ytmpd_server.py
```

Then point VLC at:

```text
http://<server>:8288/manifest?url=<youtube_url>
```

The server picks a compatible separate video/audio pair from YouTube and emits a minimal MPD whose `BaseURL`s point at the upstream URLs directly.

## Notes

- `-c copy` avoids re-encoding; if codec/container mismatch, try `-c:v copy -c:a aac`
- `-f webm` ensures proper headers so clients recognize the stream
- CPU is the bottleneck (not network), so set `WORKERS` env var accordingly
- yt-dlp returns URLs with query strings for DASH streams; ffmpeg handles them natively

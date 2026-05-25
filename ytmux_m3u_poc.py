#!/usr/bin/env python3
"""Generate a VLC-friendly M3U playlist for separate YouTube video/audio URLs."""

from __future__ import annotations

import argparse
from pathlib import Path

import yt_dlp


def _sanitize_filename(name: str, fallback: str = "video") -> str:
    if name:
        cleaned = "".join(c for c in name if c not in '<>:"/\\|?*' and ord(c) >= 32).strip()
        if cleaned:
            return cleaned
    return fallback


def _resolve_manifest(ydl: yt_dlp.YoutubeDL, manifest_url: str) -> str:
    """Resolve a DASH/HLS manifest URL to an actual media URL when possible."""
    try:
        info = ydl.extract_info(manifest_url, download=False)
    except Exception:
        return manifest_url

    formats = info.get("formats", [])
    if formats:
        return formats[0]["url"]
    return manifest_url


def get_urls(youtube_url: str) -> dict[str, str]:
    """Pick separate VP9 video + Opus audio URLs for VLC input-slave playback."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "allow_duplicate": True,
        "extract_flat": False,
        "format": (
            "bestvideo[ext=webm][vcodec*=vp9]+bestaudio[ext=webm][acodec*=opus]"
            "/bestvideo[vcodec*=vp9]+bestaudio[acodec*=opus]"
            "/bestvideo[ext=webm]+bestaudio[ext=webm]"
            "/bestvideo+bestaudio"
        ),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        requested_formats = info.get("requested_formats")
        if not requested_formats or len(requested_formats) < 2:
            raise RuntimeError("No separate video/audio formats found")

        video_format, audio_format = requested_formats[:2]
        video_url = video_format["url"]
        audio_url = audio_format["url"]

        if "manifest_url" in video_format:
            video_url = _resolve_manifest(ydl, video_url)
        if "manifest_url" in audio_format:
            audio_url = _resolve_manifest(ydl, audio_url)

        title = info.get("title") or "YouTube Video"
        return {
            "title": title,
            "video": video_url,
            "audio": audio_url,
            "filename": f"{_sanitize_filename(title)}.m3u",
            "webpage_url": info.get("webpage_url") or youtube_url,
        }


def render_m3u(urls: dict[str, str]) -> str:
    title = urls["title"].replace("\n", " ").strip()
    return "\n".join(
        [
            "#EXTM3U",
            f"#EXTINF:-1,{title}",
            f"#EXTVLCOPT:input-slave={urls['audio']}",
            urls["video"],
            "",
        ]
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an M3U playlist that lets VLC play YouTube video+audio without muxing."
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "-o",
        "--output",
        help="Output .m3u path. Defaults to '<title>.m3u' in the current directory.",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the M3U content to stdout instead of writing a file.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    urls = get_urls(args.url)
    content = render_m3u(urls)

    if args.print_only:
        print(content, end="")
        return 0

    output_path = Path(args.output or urls["filename"])
    output_path.write_text(content, encoding="utf-8")

    print(f"wrote {output_path}")
    print(f"title: {urls['title']}")
    print(f"video: {urls['video']}")
    print(f"audio: {urls['audio']}")
    print()
    print("Open the generated .m3u file in VLC.")
    print("Note: YouTube signed URLs expire, so this playlist is best treated as disposable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

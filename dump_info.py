#!/usr/bin/env python3
"""Dump YouTube video info as JSON using yt-dlp."""

import json
import sys
import yt_dlp

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=W6pryqt1dwY"

ydl_opts = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(url, download=False)

with open("info.json", "w") as f:
    json.dump(info, f, indent=2, ensure_ascii=False, default=str)

print(f"Dumped info.json ({info.get('title', 'N/A')})")

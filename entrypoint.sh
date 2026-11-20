#!/bin/sh
# yt-dlp breaks often when YouTube changes; refresh it on every start (skipped quietly when offline).
timeout 120 pip install --no-cache-dir -q -U "yt-dlp[default]" >/dev/null 2>&1 || echo "yt-dlp update skipped"
exec python /app/server.py

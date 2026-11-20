FROM python:3.12-slim-bookworm
# ffmpeg = merge/convert, deno = JS runtime yt-dlp needs for YouTube signatures
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -U "yt-dlp[default]" \
 && chown -R 1026:100 /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    XDG_CACHE_HOME=/data/.cache DENO_DIR=/data/.cache/deno HOME=/data \
    YTDLP_UPDATE_CMD="pip install --no-cache-dir -U 'yt-dlp[default]' 2>&1 | tail -3" \
    PYTHONUNBUFFERED=1
COPY entrypoint.sh /entrypoint.sh
USER 1026:100
ENTRYPOINT ["tini", "--", "sh", "/entrypoint.sh"]

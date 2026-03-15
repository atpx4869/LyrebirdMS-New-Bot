FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=

RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org/debian|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources || true; \
      sed -i "s|http://security.debian.org/debian-security|$APT_MIRROR-security|g" /etc/apt/sources.list.d/debian.sources || true; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      cron \
      tzdata \
      gcc \
      python3-dev \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN if [ -n "$PIP_INDEX_URL" ]; then pip config set global.index-url "$PIP_INDEX_URL"; fi && \
    if [ -n "$PIP_EXTRA_INDEX_URL" ]; then pip config set global.extra-index-url "$PIP_EXTRA_INDEX_URL"; fi && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/config /data/logs /data/runtime /app/runtime && \
    useradd -r -u 10001 -g root appuser && \
    chown -R appuser:root /app /data

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV CONFIG_PATH=/data/config/config.json \
    LOG_DIR=/data/logs \
    RUNTIME_DIR=/data/runtime \
    SESSION_WORKDIR=/data/runtime/pyrogram \
    ENABLE_CRON=false

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python /app/healthcheck.py || exit 1

USER appuser
ENTRYPOINT ["/entrypoint.sh"]

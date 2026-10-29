# Pinned to minor + Debian release. TODO(要確認): pin by digest (python:3.11-slim-bookworm@sha256:...)
# for reproducible builds and bump it deliberately (e.g. with Dependabot).
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Tokyo

WORKDIR /app

# Install dependencies and the package (no .env or tests are copied; see .dockerignore).
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Non-root user; /data is the persistent volume (owned by the app user so named volumes inherit it).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir /data \
    && chown bot:bot /data
USER bot
VOLUME ["/data"]

# Healthy only while the bot is ready: the alive cog touches /tmp/alive every 30s (stale > 120s).
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; sys.exit(0 if time.time()-os.stat('/tmp/alive').st_mtime < 120 else 1)"]

CMD ["python", "-m", "nomusyo.bot"]

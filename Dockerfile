# syntax=docker/dockerfile:1
########## base: offline CLI job runner (no browsers) ##########
# bookworm, not trixie (current 3.12-slim alias): trixie renamed libasound2/libcups2/libatk1.0-0 to t64 variants; bookworm keeps the browser apt list valid
FROM python:3.12-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/signals-hf
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*
COPY . /app
# CRLF self-heal: entrypoint written on Windows must exec cleanly on Linux
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh \
    && pip install --no-cache-dir -e ".[rerank]" \
    && python scripts/prefetch_models.py \
    && useradd -m -u 10001 signals \
    && mkdir -p /work/data /opt/signals-hf \
    && chown -R signals:signals /work /opt/signals-hf /app/src /app/tests
USER signals
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["doctor", "--no-network"]

########## browser: adds chromium + Xvfb for headed antibot paths ##########
FROM base AS browser
USER root
ENV SIGNALS_XVFB=1 PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
      libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && python -m playwright install chromium \
    && python -m patchright install chromium \
    && chown -R signals:signals /opt/ms-playwright
USER signals

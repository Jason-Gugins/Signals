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
    && mkdir -p /work/data /opt/signals-hf /app/data \
    && chown -R signals:signals /work /opt/signals-hf /app/src /app/tests /app/data
USER signals
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["doctor", "--no-network"]

########## browser: adds chromium + Xvfb for headed antibot paths ##########
FROM base AS browser
USER root
ENV SIGNALS_XVFB=1 PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
      libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && python -m playwright install chromium \
    && python -m patchright install chromium \
    && chown -R signals:signals /opt/ms-playwright
USER signals

########## engine-builder: compiles the native signals_antibot wheel ##########
FROM python:3.12-slim-bookworm AS engine-builder
ENV PATH=/usr/local/go/bin:/root/.cargo/bin:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build clang libclang-dev perl curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://go.dev/dl/go1.24.5.linux-amd64.tar.gz | tar -C /usr/local -xz \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
WORKDIR /app
COPY . /app
# cargo fetch FIRST: the ALPS patch needs boring-sys vendored before it can run
# (cold-cache ordering — same lesson as the WSL sandbox recipe in
# src/antibot/README.md "Build (Linux/Ubuntu)").
RUN pip install --no-cache-dir maturin \
    && cd src/antibot/engine && cargo fetch \
    && PYTHONPATH=/app python -c "from tools.build_antibot import apply_registry_patches; apply_registry_patches()" \
    && maturin build --release \
    && mkdir /wheels && mv target/wheels/signals_antibot-*.whl /wheels/

########## engine: browser + native antibot engine (tier-1 TLS parity) ##########
FROM browser AS engine
USER root
COPY --from=engine-builder /wheels/ /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/signals_antibot-*.whl && rm -rf /tmp/wheels
USER signals

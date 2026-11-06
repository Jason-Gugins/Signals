# Docker deployment (Phase 0)

**Local-first: none of this is required to run Signals locally.** Running
`python -m src.cli` from the repo venv remains the primary path. Docker is an
additional deployment option that packages the same CLI.

## Status

- Image definitions: **landed** (multi-stage `base` + `browser` targets).
- **Build record (2026-11-05):** both targets built successfully from the
  working tree at source commit `d24854a` (docker-ce 29.9.3 inside WSL2
  Ubuntu 24.04). Sizes: `signals:base` **1.05 GB**, `signals:browser`
  **3.34 GB** (chromium + patchright browsers + Xvfb libs dominate).
  One build fix vs the plan sketch: the bookworm-pin rationale comment had
  to move off the `FROM` line (inline comments are not Dockerfile syntax).
- In-container verification + first intel run: **pending** — Tasks 8–9 of
  `docs/plans/2026-10-31-phase0-containerization.md`.

## Build

```sh
docker build --target base   -t signals:base   .
docker build --target browser -t signals:browser .
```

`base` is the offline job runner (no browsers); `browser` adds chromium +
Xvfb for headed antibot paths. Both pin `python:3.12-slim-bookworm` (trixie
renamed three apt libs the browser list needs). Models are baked into
`/opt/signals-hf` at build time (`scripts/prefetch_models.py`), so runtime
never needs huggingface.co egress.

## Requirements

- **`.env` must exist** — create it from `.env.example` first. Compose reads
  it at run time (`env_file: [.env]`); with the file absent, compose errors
  opaquely.
- The container runs as non-root user `signals` (uid 10001), `WORKDIR /work`.

## Run

```sh
docker compose run --rm signals doctor --no-network     # health check
docker compose run --rm signals init                    # materialize data tree
docker compose run --rm signals intel <domain>          # a real run
# offline suite in-container (WORKDIR is /work — point pytest at /app):
docker run --rm -w /app signals:browser \
  python -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q
```

Bare CLI words are routed to `signals`; anything else (`python -m pytest`,
`bash`) is exec'd verbatim by the entrypoint.

## Storage rules (read before mounting anything)

- **Named volume only** (`signals-data:/work/data`). NEVER bind-mount the
  Windows `data/` directory into the Linux container: cross-OS SQLite WAL
  sharing (file-lock semantics + a 592 MB live DB) is a corruption hazard.
- The named volume starts EMPTY. `intel` auto-creates the account; `init`
  materializes the tree.
- **Backup**: nightly `sqlite3 .backup` inside a one-shot container, then
  copy the backup offsite (restic/rclone). A volume is not a backup.

```sh
docker run --rm -v signals-data:/work/data --entrypoint sh signals:base \
  -c 'sqlite3 /work/data/signals.db ".backup /work/data/backups/signals-$(date +%F).db"'
```

## `--env-file` caveat

`docker run --env-file .env` uses Docker's RAW parser: no quote stripping,
no variable expansion (unlike compose). Keep `.env` values unquoted if you
use raw `docker run`; compose handles both.

## Watchlist hygiene

Curated lists never bake into images: `config/lists/brands.csv` and
`config/lists/champions.csv` are excluded via `.dockerignore` (brands.csv is
gitignored-local; champions.csv is a future drop). The tracked stubs
(`competitors.txt`, `customers.txt`, `dnc.txt`, `entity_aliases.yaml`,
`xray_strings.yaml`, `README.md`) ship.

## Scheduling

Not a daemon. `watch.py`'s own docstring recommends OS cron driving one-shot
`docker compose run --rm signals watch --once` (see the compose file header
for a crontab sketch).

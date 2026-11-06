# Phase 0 Containerization — Docker-Additive Deployment Plan (2026-10-31)

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task. Built for `/goal` unattended execution: every task is offline-safe unless marked LIVE, every commit is explicit-path, and Tasks 7–9 are a skippable Docker-gated tail.

**Goal:** Make Signals deployable as a Docker container (Phase 0 of `docs/server-architecture-exploration.md` §9) — punch-list portability fixes P5/P6/P8, a multi-stage image (base + browser/Xvfb), compose stack, and in-container verification — while keeping the local Windows workflow byte-for-byte unchanged.

**Standing contract (the user's hard constraint): local-first, Docker-additive.**
- Running locally as today (Windows venv, `python -m src.cli`, `.env`, data in repo `data/`) must remain the zero-config path. Docker is an *additional* deployment option, never a requirement.
- Concretely: no default behavior changes. Every behavior difference lives behind a new env var that only the Dockerfile/compose sets (`SIGNALS_SCRAPER_PYTHON`, `HF_HOME`, `SIGNALS_XVFB`, `PLAYWRIGHT_BROWSERS_PATH`). On an untouched local machine the resolved paths are identical to today.
- Nothing under `src/` may import docker-only machinery; Docker artifacts (`Dockerfile`, `docker-compose.yml`, `docker/`, `.dockerignore`) are leaf files no runtime code reads.
- P2/P3 (SQLite WAL single-writer, in-process rate limits) are **out of scope** — structural, deferred to the Postgres phase per the architecture doc §4.2/§8.1.

**Authorization:** `/goal` unattended run. Budget ceiling: **$0.00** — every step is free (docker pulls, HuggingFace model downloads, free-source intel smoke). Two tasks are LIVE-network (Tasks 4, 9), both priced $0.00.

**Scope safety (shared working tree — re-check `git status` before every commit):**
- **Files this plan may create/modify (exhaustive):**
  - Create: `tests/test_linkedin_portability.py`, `scripts/prefetch_models.py`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `docker/entrypoint.sh`, `docs/docker.md`
  - Modify: `src/sources/linkedin_db/collector.py`, `src/identity/linkedin_ids.py`, `tests/test_linkedin_db.py`, `tests/test_linkedin_ids.py` (fixture updates, Task 2), `TODO.md`
  - Commit at Task 1: `docs/plans/2026-10-31-phase0-containerization.md` (this file; committed again at Task 10 with its Run Record filled)
- **Task dependency chain (enforce — a dispatcher must not reorder):** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10. Task 5's Dockerfile invokes `scripts/prefetch_models.py` (so 4 → 5); Tasks 7–9 record into `docs/docker.md` (so 6 → 7 → 8 → 9); Task 10 is last.
- **Off-limits (dirty from concurrent sessions as of 2026-10-31 — re-verify at runtime, this snapshot goes stale):** `BUILD_NOTES.md`, `config/decide.yaml`, `config/markets.yaml` (decide-lab session), `docs/server-architecture-exploration.md`/`.pdf` (untracked). `src/core/db.py` + `tests/test_db_core.py` were staged mid-flight when this plan was drafted and have since landed/cleaned — regardless, treat any file this plan doesn't own as off-limits. Never stage, stash, checkout, or revert files you didn't author in this run.
- **TODO.md staging hazard (Task 10):** TODO.md is the shared queue. Before `git add TODO.md`, run `git diff TODO.md`; record any foreign edits in the Run Record (never revert them), then commit immediately after your own edit.

---

## Verified context (2026-10-31 — trust these)

- **P5 hardcode confirmed, both sites:** `src/sources/linkedin_db/collector.py:82` — `exe = cwd / ".venv" / "Scripts" / "python.exe"`; `src/identity/linkedin_ids.py:48-49` — same expression in `LinkedinSlugResolver.resolve_all`. Both keep an `.exists()` guard that no-ops when the sibling checkout is absent — preserve that.
- **P5 fixture blast radius (verified — the plan's CI gate is Windows-only, so this must be handled in Task 2):** `tests/test_linkedin_db.py` builds Windows-layout fake venvs (`.venv/Scripts/python.exe`) at lines 57-58, 76-77, 105-106 and asserts the subprocess is reached; `tests/test_linkedin_ids.py`'s `scraper_env` fixture (lines 41-44) creates the same layout and asserts `assert str(exe) in argv` (line 112). On the ubuntu CI lane the helper's posix resolution would break 9 tests unless these fixtures are updated in the same task.
- **P1 needs no code change:** `src/core/runlog.py:26-39` `_resolve_logs_dir()` already falls back to `"data/logs"` when `config/default.yaml` is absent from CWD. Container pins `WORKDIR /work`; relative `data/...` paths resolve there. CI's install-smoke lane already proves `init` is CWD-independent.
- **P6 seam:** `src/retrieval/embedder.py` (`OnnxEmbedder`, lazy, revision-pinned `751bff…`, Null fallback) and `src/signals/rerank.py` (same pattern). `huggingface_hub` honors `HF_HOME` natively → setting `HF_HOME=/opt/signals-hf` in the image bakes the cache; locally (no `HF_HOME`) the user-home cache is untouched.
- **P8:** no script/CI step installs browsers — the browser image target must run `playwright install chromium` + `patchright install chromium` explicitly. Browser binaries default to `~/.cache/ms-playwright` of the build user → must set `PLAYWRIGHT_BROWSERS_PATH` to a shared path and chown it, or the non-root runtime user can't read them.
- **Docker is probably absent on this machine:** `TODO.md:1301` records `SearXNG DOCKER_ABSENT` (2026-10 probe). Tasks 7–9 must re-probe (`docker version`) and, if absent, record `deferred: DOCKER_ABSENT` and skip cleanly. Tasks 1–6 are fully valuable without Docker.
- **Offline suite gate (run after every task, before every commit):**
  `.venv/Scripts/python.exe -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q` (fall back to `python -m pytest` if no venv)
  **Known hazard:** if the suite is red and the failure traces to files this plan doesn't own (e.g. the concurrent decide-lab session's files), record "blocked by concurrent session" and STOP this plan — never revert their work.
- **Never bind-mount the Windows `data/` into a Linux container:** cross-OS SQLite WAL sharing (file-lock semantics + 592 MB live DB) is a corruption hazard (architecture doc P2). The compose stack uses a **named volume** only, and `docs/docker.md` says so loudly.
- `.gitignore` already excludes `data/`, `.env`, `tmp/` — the Docker build context must exclude them too via `.dockerignore` (592 MB DB would otherwise ship into every build).

---

## STOP gates

1. **Concurrent-session red suite:** offline suite red at HEAD with failures in `src/core/db.py`/`tests/test_db_core.py` (or any file this plan doesn't own) ⇒ record "blocked by concurrent session", stop. Do not fix-forward into foreign files.
2. **Docker absent:** `docker version` fails in Task 7 ⇒ record `deferred: DOCKER_ABSENT`, skip Tasks 7–9, proceed to Task 10.
3. **Two verification failures** on any task ⇒ record in this file + TODO.md, skip the task, continue only if later tasks don't depend on it (dependency chain: 4→5 and 6→7→8→9 — a skipped prerequisite skips its dependents).
4. **LIVE step misbehaves** (Task 9 intel run can't reach sources from the container, or exceeds ~45 min) ⇒ kill the container, record, stop that task. Suite is unaffected.
5. **Hang guard:** any single step exceeding 30 min without progress output (docker build, in-container suite) or 10 min (Task 4 model downloads) ⇒ kill it, record `deferred: TIMEOUT`, apply the same skip logic as gate 3. Prefer wrapping long steps in `timeout` where the shell supports it.
6. **Never:** bind-mount a live `data/` directory; bake `.env` into an image; run two collectors against one volume concurrently; `git add -A`/`git add .`; `reset --hard`/rebase/force-push.

---

### Task 1: Commit this plan

**Objective:** Establish the plan in-repo so `/goal` and later sessions can reference it by hash.

**Steps:**
1. `git status --porcelain` — confirm the off-limits set matches the scope section; if new dirty files appeared, add them to the plan's off-limits list first.
2. `git add docs/plans/2026-10-31-phase0-containerization.md`
3. `git commit -m "docs(plan): phase-0 containerization — docker-additive deployment, local-first contract"`

---

### Task 2: Portable scraper-venv python resolution (P5, TDD)

**Objective:** Replace the hardcoded Windows venv path with a helper that keeps `.venv/Scripts/python.exe` on Windows (byte-identical default), resolves `.venv/bin/python` on POSIX, and honors a `SIGNALS_SCRAPER_PYTHON` env override.

**Files:**
- Test (create): `tests/test_linkedin_portability.py`
- Modify: `src/sources/linkedin_db/collector.py` (helper near the top of the subprocess section, ~line 69; replace line 82 usage)

**Step 1: Write failing tests** — `tests/test_linkedin_portability.py`:

```python
"""Portable sibling-scraper venv resolution (P5): Windows default unchanged,
POSIX layout for containers, explicit env override wins."""

from pathlib import Path

import src.sources.linkedin_db.collector as collector


def test_scraper_python_windows_layout_is_unchanged(monkeypatch):
    # Local-as-is contract: on Windows the resolved path is byte-identical
    # to the previous hardcoded expression.
    monkeypatch.delenv("SIGNALS_SCRAPER_PYTHON", raising=False)
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="nt") == Path(
        "/checkouts/linkedin/.venv/Scripts/python.exe"
    )


def test_scraper_python_posix_layout_for_containers(monkeypatch):
    monkeypatch.delenv("SIGNALS_SCRAPER_PYTHON", raising=False)
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="posix") == Path(
        "/checkouts/linkedin/.venv/bin/python"
    )


def test_scraper_python_env_override_wins(monkeypatch):
    monkeypatch.setenv("SIGNALS_SCRAPER_PYTHON", "/opt/special/python")
    assert collector.scraper_python(Path("/checkouts/linkedin"), platform="nt") == Path(
        "/opt/special/python"
    )
```

**Step 2: RED** — `.venv/Scripts/python.exe -m pytest tests/test_linkedin_portability.py -q`
Expected: 3 failed — `AttributeError: ... has no attribute 'scraper_python'`.

**Step 3: Implement** — in `src/sources/linkedin_db/collector.py`, add above `request_linkedin_deepen`:

```python
def scraper_python(cwd: Path, platform: str | None = None) -> Path:
    """Resolve the sibling scraper checkout's venv python.

    Windows layout (.venv/Scripts/python.exe) stays the default so local
    behavior is unchanged; POSIX containers resolve .venv/bin/python. An
    explicit SIGNALS_SCRAPER_PYTHON wins outright (custom layouts, CI).
    Callers keep their own .exists() no-op guard.
    """
    import os

    env_exe = os.environ.get("SIGNALS_SCRAPER_PYTHON")
    if env_exe:
        return Path(env_exe)
    plat = platform if platform is not None else os.name
    rel = ".venv/Scripts/python.exe" if plat == "nt" else ".venv/bin/python"
    return cwd / rel
```

Then in `request_linkedin_deepen` replace `exe = cwd / ".venv" / "Scripts" / "python.exe"` with `exe = scraper_python(cwd)`. Keep the surrounding warning/guard lines untouched.

**Step 4: Fix the existing fixtures (the ubuntu CI lane depends on it)** — verified blast radius: `tests/test_linkedin_db.py` builds Windows-layout fake venvs at lines 57-58, 76-77, 105-106, and `tests/test_linkedin_ids.py`'s `scraper_env` fixture (lines 41-44) asserts `assert str(exe) in argv` (line 112). On the ubuntu CI lane `scraper_python` resolves `.venv/bin/python`, which those fixtures never create — 9 tests would go red after push. Update the fixtures to create the layout `scraper_python` resolves on the host: build BOTH layouts (`.venv/Scripts/` and `.venv/bin/` each with a dummy python file) or, in `test_linkedin_ids.py`, derive the asserted exe via `collector.scraper_python(cwd)`. Pick the smallest edit per file; keep every assertion's meaning.

**Step 5: GREEN + suite** — `pytest tests/test_linkedin_portability.py tests/test_linkedin_db.py tests/test_linkedin_ids.py -q` (all passed), then the full offline gate. Expected: green locally (Windows resolves the byte-identical Windows path).

**Step 6: Commit** — `git add tests/test_linkedin_portability.py tests/test_linkedin_db.py tests/test_linkedin_ids.py src/sources/linkedin_db/collector.py`
`git commit -m "fix(sources): sibling-scraper venv python is platform-portable — env override + POSIX layout"`

---

### Task 3: Same fix in the slug resolver

**Objective:** Point `LinkedinSlugResolver` at the helper so both subprocess seams share one resolution (DRY).

**Files:**
- Modify: `src/identity/linkedin_ids.py:48-49` and its imports (`from src.sources.linkedin_db.collector import scraper_python`)

**Steps:**
1. Replace `exe = cwd / ".venv" / "Scripts" / "python.exe"` with `exe = scraper_python(cwd)`. No other line changes (the `not cwd.exists() or not exe.exists()` guard at line 50 stays).
2. Import-cycle check: `grep -n "import" src/sources/linkedin_db/collector.py` — confirm it imports nothing from `src/identity/` (expected clean; collector mirrors, identity imports collector).
3. Run offline gate. Expected: green (this path is guarded no-op without the sibling checkout).
4. Commit: `git add src/identity/linkedin_ids.py`
   `git commit -m "fix(identity): slug resolver uses portable scraper python (shared helper)"`

---

### Task 4: Model prefetch script (P6) — LIVE, priced $0.00

**Objective:** One script that warms both revision-pinned MiniLM models into the HF cache, refusing to lie on failure — the Dockerfile bakes it; locally it's optional and lands in the same user-home cache as today.

**Files:**
- Create: `scripts/prefetch_models.py`

**Step 1: Confirm accessor names** (verified 2026-10-31 — re-confirm with `grep -n "^def get_\|^class " src/retrieval/embedder.py src/signals/rerank.py` before writing): `get_embedder()` / `NullEmbedder` in embedder.py; `get_scorer()` / `NullScorer` / `score_pairs(query, docs)` in rerank.py. Use exactly these in the script below.

**Step 2: Write the script:**

```python
"""Prefetch the two revision-pinned MiniLM models into the HF cache.

Used at Docker build time (HF_HOME baked into the image) so the container
never touches huggingface.co at runtime; also runnable locally to pre-warm.
Fails loudly: a Null fallback or a download error exits 1 — a silent no-op
prefetch defeats its own purpose.
"""
import sys

from src.retrieval.embedder import NullEmbedder, get_embedder
from src.signals.rerank import NullScorer, get_scorer


def main() -> int:
    emb = get_embedder()
    if isinstance(emb, NullEmbedder):
        print("FAIL: embedder degraded to NullEmbedder — install signals[rerank]", file=sys.stderr)
        return 1
    vec = emb.embed(["prefetch probe"])
    if not vec or len(vec[0]) != 384:
        print(f"FAIL: embedder returned {vec!r}", file=sys.stderr)
        return 1
    print(f"embedder ok: {getattr(emb, 'model', emb.__class__.__name__)}")

    rr = get_scorer()
    if isinstance(rr, NullScorer):
        print("FAIL: reranker degraded to NullScorer — install signals[rerank]", file=sys.stderr)
        return 1
    # get_scorer() alone downloads nothing: the hf_hub fetch is lazy inside
    # _ensure_loaded(), triggered only by score_pairs() (rerank.py:76-79).
    # A type-only check would bake the embedder and silently skip the
    # reranker model — the exact failure this script exists to prevent.
    scores = rr.score_pairs("prefetch probe", [("prefetch probe", "probe summary")])
    if not scores:
        print(f"FAIL: scorer returned {scores!r}", file=sys.stderr)
        return 1
    print(f"scorer ok: {type(rr).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Accessor names verified 2026-10-31 (`get_embedder`/`NullEmbedder` at embedder.py:145/39; `get_scorer`/`NullScorer`/`score_pairs(query, docs)` at rerank.py:123/34/76) — no grep-guessing remains. Do not fake-test the download; this task's verification IS the live run.

**Step 3: Run it (LIVE, network, $0, 10-min hang guard):** `.venv/Scripts/python.exe scripts/prefetch_models.py`
Expected: `embedder ok:` + `scorer ok:` lines, exit 0, both revision-pinned models (~100 MB total) in the local user-home cache (same cache normal runs use — local workflow unchanged, just pre-warmed).

**Step 4: Offline suite gate** (script isn't imported by `src/`, but gate anyway — it's the commit precondition).

**Step 5: Commit:** `git add scripts/prefetch_models.py`
`git commit -m "feat(scripts): model prefetch — pins embedder+reranker warm in the HF cache"`

---

### Task 5: Dockerfile, .dockerignore, entrypoint

**Objective:** Ship the multi-stage image definition (P8, P4, P6-bake, tini, non-root) with the base target browser-free and the browser target adding chromium + Xvfb.

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker/entrypoint.sh`

**`Dockerfile`** (complete):

```dockerfile
# syntax=docker/dockerfile:1
########## base: offline CLI job runner (no browsers) ##########
FROM python:3.12-slim-bookworm AS base  # trixie (current 3.12-slim alias) renamed libasound2/libcups2/libatk1.0-0 to t64 variants; bookworm keeps the browser apt list valid
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
```

Notes baked into the design: base pins **bookworm** (see FROM comment — a trixie build fails on three renamed libs); editable install `/app` keeps the package-relative `config/` resolution (`config.py` walks `Path(__file__)` up to repo root) working; `WORKDIR /work` pins all CWD-relative `data/...` state onto the volume mount point; models bake into `/opt/signals-hf` so runtime never needs egress; `/app/src` + `/app/tests` are chowned so the non-root user can write `__pycache__` (the in-container suite recompiles 300+ files otherwise); pytest stays in the image (core dep — P7 tolerated) which also enables the in-container suite in Task 8; `tests/` must NOT be excluded by `.dockerignore`.

**`.dockerignore`** (complete):

```
.git
.venv
data
tmp
PDFS
.zcode
.zcodeignore
.hermes
.pytest_cache
.ruff_cache
signals.egg-info
__pycache__
docs
*.pdf
rfc7541_plain.txt
.env
BUILD_NOTES.md
"sample companies.txt"
config/lists/brands.csv
config/lists/champions.csv
```

Watchlist hygiene (verified tracked set 2026-10-31: `config/lists/{README.md, competitors.txt, customers.txt, dnc.txt, entity_aliases.yaml, xray_strings.yaml}` are tracked and ship; `brands.csv` is gitignored-local and `champions.csv` is the user's future drop — `*.csv`-style curated lists never bake into images).

**`docker/entrypoint.sh`** (complete):

```sh
#!/bin/sh
# Xvfb wrapper for headed-browser paths (ghost/g2-selfcheck/CF fallback).
# Only the browser target sets SIGNALS_XVFB=1; base stays lean.
# Passthrough rule: bare CLI invocations are routed to `signals`; anything
# else (python -m pytest, bash, ...) is exec'd verbatim so the image stays a
# general-purpose runner for verification steps like Task 8's.
set -e
case "$1" in
  ""|doctor|init|status|accounts|seed|resolve|collect|score|brief|export|intel|run|watch|index|prune|digest)
    if [ "$SIGNALS_XVFB" = "1" ] && [ -z "$DISPLAY" ] && command -v xvfb-run >/dev/null 2>&1; then
      exec xvfb-run -a signals "$@"
    fi
    exec signals "$@" ;;
  *) exec "$@" ;;
esac
```

**Verification (static — no Docker needed):**
1. `git status` off-limits check.
2. Sanity-grep the paths the files reference exist: `ls scripts/prefetch_models.py docker/entrypoint.sh`, `grep -n "rerank" pyproject.toml`.
3. Offline suite gate (unchanged code — should be trivially green).

**Commit:** `git add Dockerfile .dockerignore docker/entrypoint.sh`
`git commit -m "feat(docker): multi-stage image — base + browser/Xvfb targets, baked models, non-root, tini"`

---

### Task 6: compose stack + operator runbook

**Objective:** The single-host topology from architecture doc §3.3 — one-shot jobs via `docker compose run`, named volume, cron recommended over a daemon — documented so an operator (or future you) can run it without re-deriving anything.

**Files:**
- Create: `docker-compose.yml`, `docs/docker.md`

**`docker-compose.yml`** (complete):

```yaml
# Local-as-is note: this stack is OPTIONAL. Running `python -m src.cli` in the
# repo venv remains the primary path; this file only packages the same CLI.
services:
  signals:
    image: signals:browser
    build:
      context: .
      target: browser
    env_file: [.env]           # read at run time; never baked into the image
    working_dir: /work
    volumes:
      - signals-data:/work/data  # NAMED VOLUME ONLY — never bind-mount a live
                                 # data/ dir (cross-OS SQLite WAL corruption)
    command: ["doctor", "--no-network"]
    # Scheduler is deliberately NOT a long-running service (watch.py's own
    # docstring recommends OS cron). Drive one-shots from host cron:
    #   */60 * * * * docker compose -f /path/to/docker-compose.yml run --rm signals watch --once
    #   7 3 * * *   docker compose -f /path/to/docker-compose.yml run --rm signals prune
volumes:
  signals-data:
```

**`docs/docker.md`** must cover (short runbook, ~80 lines): build both targets; run doctor / offline suite / init / a collect; the named-volume rule and why; nightly `sqlite3 .backup` inside a one-shot container + offsite copy; the local-first statement ("none of this is required to run Signals locally"); the `.env` requirement (compose errors opaquely when `.env` is absent — "create .env from .env.example first"); the `docker run --env-file .env` caveat (raw parse — no quote stripping or variable expansion, unlike compose; keep `.env` values unquoted); watchlist hygiene (curated `config/lists/*.csv` never bake into images; tracked stubs ship); current status header to be filled by Tasks 7–9 (build record with source commit hash, verification record) or the DOCKER_ABSENT deferral note.

**Verification:** offline suite gate; `docker compose config` is NOT runnable without Docker — static YAML sanity only (`python -c "import yaml,sys; yaml.safe_load(open('docker-compose.yml'))"` expected no output).

**Commit:** `git add docker-compose.yml docs/docker.md`
`git commit -m "feat(docker): compose stack + operator runbook — named volume, cron-driven one-shots"`

---

### Task 7: Build both image targets — Docker-gated

**Objective:** Prove the image builds; record sizes. Skips cleanly if Docker is absent.

**Steps:**
1. Probe: `docker version` (both client and server sections). Failure ⇒ record `deferred: DOCKER_ABSENT (re-probed 2026-10-31; precedent TODO.md:1301)` in this file's Run Record and `docs/docker.md`, **skip Tasks 8–9**, go to Task 10, and use this commit message instead of step 5's: `git commit -m "docs(docker): DOCKER_ABSENT deferral — image tail skipped, offline tail delivered"`
2. `docker build --target base -t signals:base .` — expected success; first build pulls python:3.12-slim-bookworm and pip wheels (LIVE, free, ~5–10 min, 30-min hang guard).
3. `docker build --target browser -t signals:browser .` — expected success (~5 min, cached base).
4. Record both image sizes (`docker images signals`) AND the source commit hash (`git rev-parse HEAD`) in `docs/docker.md`'s status header — the image is built from a working tree, so pin the commit it was built from.
5. Commit: `git add docs/docker.md` → `git commit -m "docs(docker): image build record — both targets built, sizes recorded"`

---

### Task 8: In-container offline verification — Docker-gated

**Objective:** The image proves itself without touching live sources.

**Steps (all must pass; any failure twice ⇒ record + skip):**
1. `docker run --rm signals:browser doctor --no-network` — expected: no FAIL lines (WARN allowed). Record the exit code; if WARN-only still exits non-zero, note it in `docs/docker.md` (compose's default `command` stays doctor regardless).
2. Offline suite in-container (30-min hang guard):
   `docker run --rm -w /app signals:browser python -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q` — expected: same pass count as the host gate. (`-w /app` is required: the image's WORKDIR is /work, which has no pyproject.toml or tests/ — pytest would collect zero tests and exit 5.)
3. Fresh-volume init: `docker volume create smoke-vol && docker run --rm -v smoke-vol:/work/data signals:browser init` then `docker run --rm -v smoke-vol:/work/data signals:browser status` — expected: clean tree, no traceback. `docker volume rm smoke-vol` after.
4. Dry-run smoke (no fetch): `docker run --rm -v signals-data:/work/data signals:browser --dry-run collect` — expected: dry-run report, exit 0.
5. Append the full record to `docs/docker.md`; commit:
   `git add docs/docker.md` → `git commit -m "docs(docker): in-container verification — doctor/pytest/init/dry-run green"`

---

### Task 9: One real intel run in-container — Docker-gated, LIVE, priced $0.00

**Objective:** The architecture doc's Phase-0 exit criterion: a full `intel` run against the named volume, free sources only.

**Steps:**
1. Egress probe (5 s, fails fast before the long run): `docker run --rm signals:browser python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"` — expected exit 0. Failure ⇒ record "container egress blocked (DNS/proxy)" in `docs/docker.md`, commit `git commit -m "docs(docker): intel live run skipped — container egress probe failed"`, and stop this task.
2. Pick the domain operationally (the container volume `signals-data` starts EMPTY — intel auto-creates the account, so no seeding is needed): on the host, run
   `.venv/Scripts/python.exe -c "import sqlite3; con=sqlite3.connect('file:data/signals.db?mode=ro', uri=True); print(con.execute('SELECT domain, COUNT(*) FROM documents WHERE domain IS NOT NULL GROUP BY domain ORDER BY COUNT(*) LIMIT 1').fetchall())"`
   — the smallest-domain pick. (`documents.domain` is indexed but nullable, db.py:158; the WHERE clause keeps a NULL group from winning.)
3. `docker run --rm -v signals-data:/work/data --env-file .env signals:browser intel <that domain>` — expected: completes in ≤ ~30–45 min (large accounts run ~30 min per TODO.md:1030).
4. Record in `docs/docker.md`: domain, wall clock, documents/signals counters from the run tail.
5. Budget/stop: any network-refused error from inside the container ⇒ record and stop this task (Task 8 already proved the image; this is the live bonus). Commit: `git add docs/docker.md` → `git commit -m "docs(docker): first in-container intel run — domain and counters recorded"`

---

### Task 10: TODO.md bookkeeping (house format)

**Objective:** Honest morning-handoff record.

**Steps:**
1. Add/flip the TODO.md entry following the established house format (see the DELIVERED entry at TODO.md:12): a `## Phase 0 containerization — DELIVERED (2026-10-31)` section heading; a `**Status: DELIVERED (2026-10-31)** — N commits across M tasks` line with the provenance parenthetical (`(overnight /goal run from docs/plans/2026-10-31-phase0-containerization.md; offline suite green at every batch gate). One commit per task, subagent TDD throughout.`); a `- [x] **<item>** (`<hash>`) — <what shipped>` checklist with one item per task; the DOCKER_ABSENT/TIMEOUT deferrals if they happened; suite status at HEAD; and `Remaining fresh-decision territory:` naming the single most important next decision (expected: "install Docker Desktop to run the deferred Tasks 7–9, or deploy the image on the target VPS directly"). Update the top-of-file `Last reviewed:` line.
2. Fill this plan's Run Record table below — it ships in the same commit.
3. Offline suite gate one last time. Check `git diff TODO.md` for foreign edits from concurrent sessions before staging (record them; never revert).
4. Commit: `git add TODO.md docs/plans/2026-10-31-phase0-containerization.md` → `git commit -m "docs(todo): phase-0 containerization record — run record filled"`

---

## Run Record (fill during execution)

| Task | Outcome | Commit | Notes |
| --- | --- | --- | --- |
| 1 | DELIVERED | 55532a8 | prereqs first: dirty decide-lab files landed (5d76ac5, eb7e832); arch doc 9caf56b; WSL2+docker installed |
| 2 | DELIVERED | e2d7c9f | scraper_python helper; RED verified via HEAD-swap; fixtures both-layout |
| 3 | DELIVERED | ee90365 | slug resolver on shared helper; cycle check clean |
| 4 | DELIVERED | 51657fe | LIVE prefetch green: embedder ok + scorer ok (OnnxScorer), models warm |
| 5 | DELIVERED | 2758b81 | + fixes 08c3f7b (inline comment off FROM), cf49b72 (xauth), a515efa (/app/data, **/__pycache__) |
| 6 | DELIVERED | 8115623 | compose + runbook; YAML sanity green |
| 7 | DELIVERED | adc5570 | built from d24854a; base 1.05 GB, browser 3.34 GB (docker in WSL2, not DOCKER_ABSENT) |
| 8 | DELIVERED | 35ed0b7 | suite 88→56→1→0 (4 runs; root causes: xauth, /app/data, stale __pycache__ bytecode, 3rd Windows-only fixture 17eb861); smokes green; entrypoint -\* routing |
| 9 | DELIVERED | 46a24be | briefcatch.com, exit 0, ~5 min; honest zeros ($0 posture, no --env-file); decide-attached variant deliberately skipped per ceiling |
| 10 | DELIVERED | (this commit) | TODO entry + record; suite green host+container at HEAD |

Environment note: docker-ce 29.9.3 runs INSIDE WSL2 Ubuntu 24.04 (installed
2026-11-05, after the plan drafted DOCKER_ABSENT as likely); docker
commands execute via `wsl -e bash -lc`.

## Risks / open questions

- **Docker absence is likely** (DOCKER_ABSENT precedent) — by design, the run still ships Tasks 1–6 + 10 and the image is buildable anywhere Docker exists (VPS, CI).
- **Accessor names verified 2026-10-31:** `get_embedder`/`NullEmbedder` (embedder.py:145/39) and `get_scorer`/`NullScorer`/`score_pairs(query, docs)` (rerank.py:123/34/76) — Task 4's script uses them directly; the reranker downloads ONLY when `score_pairs` is called, so that probe is load-bearing, not cosmetic.
- **doctor exit-code semantics** in Task 8 — WARN-vs-FAIL exit behavior recorded, not assumed.
- **Debian trixie resolved by pin:** `python:3.12-slim` now aliases trixie, where `libasound2`/`libcups2`/`libatk1.0-0` are renamed to the `t64` variants and the browser apt list would fail the whole transaction — base pins `python:3.12-slim-bookworm`. If bookworm is ever EOL, replace the hand list with `playwright install-deps chromium`.
- **Ubuntu CI lane resolved by fixture updates:** Task 2 fixes the Windows-layout fixtures in `tests/test_linkedin_db.py` + `tests/test_linkedin_ids.py` (9 tests would otherwise go red on ubuntu).
- **Not in scope (explicit):** P2/P3 storage model, P7 dep splitting, K8s manifests, AWS wiring, the Linux antibot-engine build (P10) — all deferred per the architecture doc's triggers.

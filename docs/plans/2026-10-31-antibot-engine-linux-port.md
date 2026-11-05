# Antibot Engine Linux Port — Spike Plan (2026-10-31)

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task. Built for `/goal` unattended execution: every task is offline-safe unless marked LIVE (all LIVE steps priced $0.00), every commit is explicit-path, and the Linux-build tail degrades cleanly when no Linux environment exists.

**Goal:** Prove the native `signals_antibot` engine (Rust + vendored BoringSSL + PyO3) builds and passes its full verification matrix on Linux/Ubuntu — then bake it into the Docker image as an `engine` target — removing the last Windows-only artifact from the deployment story (architecture doc punch-list item P10, staged as "Phase 0.5").

**Standing contract: additive, Windows-first unchanged.**
- The Windows build path (`tools/build_antibot.py` from a VS dev prompt) remains byte-for-byte the documented preferred path; the helper refactor must keep Windows behavior identical (tests pin it).
- Nothing under `src/` runtime code changes. Deliverables are: a platform-aware build helper, tests, a Linux build doc section, a Docker `engine` target, and measured Linux verification results.
- The engine stays optional at runtime — an unbuilt engine is the documented healthy state (`doctor` WARN, curl_cffi fallback).
- **Depends on Phase 0** (`docs/plans/2026-10-31-phase0-containerization.md`): its Dockerfile/.dockerignore/docs/docker.md must exist for the Docker tasks. If Phase 0 hasn't landed, those tasks defer; the helper refactor and docs tasks are independent.

**Authorization:** `/goal` unattended run. Budget ceiling **$0.00** — LIVE steps are package downloads (apt/rustup/Go/crates.io) and the opt-in `tls.peet.ws` parity test; all free.

**Scope safety (shared working tree — re-check `git status` before every commit):**
- **Files this plan may create/modify (exhaustive):**
  - Create: `tests/test_antibot_build_helper.py`
  - Modify: `tools/build_antibot.py`, `src/antibot/README.md` (new "Build (Linux/Ubuntu)" section), `Dockerfile` + `.dockerignore` (engine target + target-dir exclusion), `docs/docker.md` (engine-target record), `TODO.md`
  - Commit at Task 1: this plan file; committed again at Task 7 with Run Record filled
- **Off-limits (dirty from concurrent sessions as of 2026-10-31 — re-verify at runtime):** `BUILD_NOTES.md` (concurrently modified — and it is the engine's internals ledger, so Linux notes go in `src/antibot/README.md` instead), `config/decide.yaml`, `config/markets.yaml`, `docs/server-architecture-exploration.md`/`.pdf` (untracked). Never stage, stash, checkout, or revert files you didn't author in this run.
- **Task dependency chain:** 1 → 2 → 3 → {4, 5 (WSL2-gated), 6 (Docker-gated, Phase-0-dependent)} → 7. Tasks 4–5 and 6 are mutually independent; Task 7 is last.
- **WSL2 sandbox hygiene:** the Linux build runs in a sandbox copy at `~/signals-linux` inside WSL, **never** against the Windows repo checkout and never under `/mnt/c` (9P I/O + SQLite WAL hazard, architecture doc P2). Code is edited in the Windows repo (the source of truth) and rsynced one-way into the sandbox. Nothing is committed from the sandbox.

---

## Verified context (2026-10-31 — trust these)

- **The crate is platform-clean:** zero `cfg(windows)`/`cfg(target_os)` gates in all five Rust sources (`lib.rs`, `tls.rs`, `session.rs`, `pool.rs`, `eyeballs.rs`). Dependencies are cross-platform with Linux as a first-class target: `pyo3 0.22`, `boring`/`boring-sys 4` (resolved 4.22.0, pinned by `Cargo.lock`), `brotli`, `flate2`, `serde_json`, `foreign-types`.
- **Python-side discovery is OS-agnostic:** `src/antibot/python/transport.py:49` does a plain `import signals_antibot` in a try/except → curl_cffi fallback; maturin installs the platform-standard extension name (`.pyd` on Windows, `.so` on Linux). `engine_available` and `doctor`'s `antibot_engine` WARN (not FAIL) semantics already exist.
- **The helper's full Windows-ism inventory** (`tools/build_antibot.py`, 90 lines): (1) `EXTRA_PATH` machine-specific PATH lines, (2) `LIBCLANG_PATH` hardcoded to `C:\Program Files\LLVM\bin`, (3) `apply_registry_patches()` globs `~\.cargo\registry\src\*\boring-sys-*\...` with **backslashes** (Windows-glob-only), (4) venv paths `.venv/Scripts/{maturin.exe,python.exe}`. The vcvars64/MSVC step does not exist on Linux — the default toolchain replaces it. The **ALPS patch is load-bearing**: rewrites `TLSEXT_TYPE_application_settings 17513 → 17613` idempotently on every build; skipping it silently puts the stale codepoint on the wire (parity symptom).
- **NASM is a Windows-only prereq.** BoringSSL on Linux x86_64 generates assembly via perl (preinstalled on Ubuntu). The Windows prereq list becomes: rustup (stable), Go 1.24+ (tarball — bookworm's apt `golang-go` is 1.19, too old; the Windows machine built with Go 1.24+ per BUILD_NOTES), `build-essential`, `cmake`, `ninja-build`, `clang` + `libclang-dev` (bindgen), `perl`, `curl`.
- **Fingerprint parity is expected to transfer:** the ClientHello is byte-emergent from the same vendored BoringSSL driven by the same data table — TLS handshake bytes are host-OS-independent. The verification stack runs unmodified on Linux: offline engine suites (`tests/test_antibot_engine.py` + cookies/transport/ghost/routing/integration), fixture parity vs `tests/fixtures/antibot/chrome_fingerprint_reference.json`, and opt-in live parity (`-m antibot_live`, tls.peet.ws). Engine-file tests auto-skip via importorskip when unbuilt.
- **First BoringSSL compile: 2–5 min** (Windows measurement); cold cargo cache in Docker may take longer — hence the 30-min hang guard.
- **Known Dockerfile defect to fix here regardless:** `.dockerignore` (from Phase 0) does not exclude `src/antibot/engine/target/` — **566 MB** of gitignored-but-on-disk build artifacts that `COPY . /app` would ship into every build context.
- **Phase 0 artifacts this plan consumes:** `Dockerfile` (base/browser targets, `python:3.12-slim-bookworm`), `.dockerignore`, `docs/docker.md`. Builder stage must use the **same base** so the wheel's glibc matches the runtime image.
- **Windows offline gate (run after every task, before every commit):**
  `.venv/Scripts/python.exe -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q` (fall back to `python -m pytest` if no venv). The WSL2 sandbox suite is an *additional* Linux verification, never a replacement — commits happen in the Windows repo.

---

## Human prerequisites (cannot be automated unattended)

- **WSL2 Ubuntu 24.04 installed** (24.04 ships Python 3.12; on 22.04 the sandbox needs deadsnakes). Setup per the standard manual LTSC path (enable `Microsoft-Windows-Subsystem-Linux` + `VirtualMachinePlatform`, reboot, WSL msix/kernel from GitHub, distro appx). `/goal` must never reboot — if WSL2 is absent, that's a deferral, not a task.
- **Docker present** (via WSL2 `docker-ce` or any Linux-side engine) for Task 6.
- **~4 GB free** in the WSL filesystem (rustup + Go + cargo target tree).
- Neither present ⇒ Tasks 4–6 defer; Tasks 1–3 + 7 still ship (platform-aware helper + tests are valuable on Windows alone).

---

## STOP gates

1. **No Linux environment** after Task 2's probe ⇒ record `deferred: NO_LINUX_ENV` in Run Record + TODO.md, run Task 3 (Windows-only helper work) and Task 7, stop.
2. **Two verification failures** on any task ⇒ record in this file + TODO.md, skip the task, apply the dependency chain.
3. **Hang guard:** any single step exceeding 30 min without progress output (first BoringSSL compile, docker builds, in-container suites) or 15 min (rustup/Go/crates downloads) ⇒ kill it, record `deferred: TIMEOUT`.
4. **LIVE step misbehaves:** parity test fails against `tls.peet.ws` for *network* reasons ⇒ record, stop that task (the fixture-based parity result still stands; don't conflate the two).
5. **Concurrent-session red suite:** Windows offline gate red at HEAD with failures in files this plan doesn't own ⇒ record "blocked by concurrent session", stop.
6. **Never:** build under `/mnt/c`; commit binaries (wheels/`.so` stay out of git — artifacts are rebuilt, not shipped); touch `BUILD_NOTES.md`; `git add -A`/`git add .`; reboot or modify WSL/BIOS settings.

---

### Task 1: Commit this plan

**Steps:**
1. `git status --porcelain` — confirm the off-limits set; extend this file's list if new dirty files appeared.
2. `git add docs/plans/2026-10-31-antibot-engine-linux-port.md`
3. `git commit -m "docs(plan): antibot engine linux port spike — platform-aware build helper, docker engine target"`

---

### Task 2: Linux environment probe + build-context fix

**Objective:** Know which Linux paths are available tonight, and fix the 566 MB build-context defect on the Windows side regardless.

**Steps:**
1. Probe WSL2: `wsl.exe -l -q` (distros listed?) then `wsl -e bash -lc "lsb_release -rs; python3 --version"` — record distro + version. Expected ideal: Ubuntu 24.04, Python 3.12.
2. Probe Docker: `docker version` (client + server). Record which of {WSL2, Docker} exist. Neither ⇒ write `deferred: NO_LINUX_ENV` into Run Record now (Tasks 4–6 will defer per gate 1; Task 3 still runs).
3. Fix the build-context defect: add one line to `.dockerignore`:
   ```
   src/antibot/engine/target
   ```
4. Windows offline gate (unchanged code — trivially green).
5. Commit: `git add .dockerignore docs/plans/2026-10-31-antibot-engine-linux-port.md`
   `git commit -m "fix(docker): exclude 566MB engine target tree from build context"`

---

### Task 3: Platform-aware build helper (TDD, Windows-runnable)

**Objective:** Make `tools/build_antibot.py` run unchanged on both platforms: cross-platform registry-patch glob, optional Windows-only PATH plumbing, per-OS venv paths — with the patch logic testable via injection.

**Files:**
- Test (create): `tests/test_antibot_build_helper.py`
- Modify: `tools/build_antibot.py`

**Step 1: Write failing tests** — `tests/test_antibot_build_helper.py`:

```python
"""Platform-aware build helper for the native engine (P10 port).

The ALPS registry patch is load-bearing (stale codepoint = detectable
fingerprint mismatch), so its logic gets direct tests with an injected
registry root — runnable on Windows without a real cargo registry.
"""

from pathlib import Path

import tools.build_antibot as helper


def _fake_registry(tmp_path: Path) -> Path:
    header_dir = (
        tmp_path / "registry" / "src" / "index.crates.io-6f17d22bba15001f"
        / "boring-sys-4.22.0" / "deps" / "boringssl" / "src" / "include" / "openssl"
    )
    header_dir.mkdir(parents=True)
    header = header_dir / "tls1.h"
    header.write_text(
        "#define TLSEXT_TYPE_application_settings 17513\n", encoding="utf-8"
    )
    return tmp_path / "registry"


def test_registry_patch_rewrites_and_is_idempotent(tmp_path, capsys):
    root = _fake_registry(tmp_path)
    helper.apply_registry_patches(root)
    first = capsys.readouterr().out
    assert "Patched ALPS codepoint" in first
    header = next(root.glob("src/*/boring-sys-*/deps/boringssl/src/include/openssl/tls1.h"))
    assert "17613" in header.read_text(encoding="utf-8")
    helper.apply_registry_patches(root)
    assert "already patched" in capsys.readouterr().out


def test_registry_patch_noop_when_no_boring_sys(tmp_path, capsys):
    (tmp_path / "registry" / "src").mkdir(parents=True)
    helper.apply_registry_patches(tmp_path / "registry")
    assert "already patched (or not found)" in capsys.readouterr().out


def test_venv_paths_follow_platform():
    posix = helper.venv_paths(Path("/repo"), platform="posix")
    nt = helper.venv_paths(Path("/repo"), platform="nt")
    assert posix == (Path("/repo/.venv/bin/maturin"), Path("/repo/.venv/bin/python"))
    assert nt[0] == Path("/repo/.venv/Scripts/maturin.exe")
    assert nt[1] == Path("/repo/.venv/Scripts/python.exe")
```

**Step 2: RED** — `.venv/Scripts/python.exe -m pytest tests/test_antibot_build_helper.py -q`
Expected: failures — `apply_registry_patches()` takes no argument / `venv_paths` missing.

**Step 3: Implement** — rewrite `tools/build_antibot.py`:

```python
"""Build helper for the native signals_antibot engine (Rust + BoringSSL + PyO3).

Cross-platform (Windows + Linux/Ubuntu). Linux prereqs:
  rustup (stable), Go 1.24+ tarball, build-essential, cmake, ninja-build,
  clang, libclang-dev, perl. NASM is Windows-only (Linux asm is perl-based).

Usage (from repo root):
  Windows (VS dev prompt): .venv\\Scripts\\python.exe tools\\build_antibot.py
  Linux/WSL2:              .venv/bin/python tools/build_antibot.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO / "src" / "antibot" / "engine"

# Windows-only toolchain paths (Go/NASM/LLVM outside the default PATH).
WINDOWS_EXTRA_PATH = [
    r"C:\Program Files\Go\bin",
    r"C:\Program Files\LLVM\bin",
    r"C:\Users\Jason\nasm\nasm-2.16.03",
    r"C:\Users\Jason\.cargo\bin",
]


def apply_registry_patches(cargo_registry_root: Path | None = None) -> None:
    """Apply our Chrome-parity patches to the vendored boring-sys BoringSSL.

    The cargo registry source is ephemeral (refreshed by `cargo update` /
    registry clean), so re-apply on every build. Idempotent.
    Patches (see BUILD_NOTES.md 'Vendored BoringSSL patch'):
      - TLSEXT_TYPE_application_settings 17513 -> 17613 (Chrome 151+ emits the
        final IANA codepoint; upstream BoringSSL still sends the legacy draft).
    Forward-slash globs work on both platforms.
    """
    import glob
    import re

    root = Path(cargo_registry_root) if cargo_registry_root else Path.home() / ".cargo"
    pattern = str(
        root / "registry" / "src" / "*" / "boring-sys-*"
        / "deps" / "boringssl" / "src" / "include" / "openssl" / "tls1.h"
    )
    for header in glob.glob(pattern):
        text = Path(header).read_text(encoding="utf-8", errors="replace")
        patched = re.sub(
            r"#define TLSEXT_TYPE_application_settings 17513\b",
            "#define TLSEXT_TYPE_application_settings 17613",
            text,
        )
        if patched != text:
            Path(header).write_text(patched, encoding="utf-8")
            print(f"Patched ALPS codepoint 17513 -> 17613 in {header}")
        else:
            print(f"ALPS codepoint already patched (or not found): {header}")


def venv_paths(repo: Path, platform: str | None = None) -> tuple[Path, Path]:
    """(maturin, python) executable paths inside the repo venv, per OS."""
    plat = platform if platform is not None else os.name
    if plat == "nt":
        return repo / ".venv" / "Scripts" / "maturin.exe", repo / ".venv" / "Scripts" / "python.exe"
    return repo / ".venv" / "bin" / "maturin", repo / ".venv" / "bin" / "python"


def main() -> int:
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = os.pathsep.join(WINDOWS_EXTRA_PATH) + os.pathsep + env.get("PATH", "")
        env["LIBCLANG_PATH"] = r"C:\Program Files\LLVM\bin"

    apply_registry_patches()

    maturin, venv_python = venv_paths(REPO)
    maturin = str(maturin) if maturin.exists() else "maturin"

    print(f"Building engine in {ENGINE_DIR} ...")
    result = subprocess.run([maturin, "develop", "--release"], cwd=ENGINE_DIR, env=env)
    if result.returncode != 0:
        print("Build FAILED — see src/antibot/README.md for troubleshooting.", file=sys.stderr)
        return result.returncode

    print("\nSanity check:")
    check = subprocess.run(
        [str(venv_python), "-c",
         "import signals_antibot as m; print('engine_version:', m.engine_version()); "
         "print('tls:', m.tls_library_version())"]
    )
    return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
```

Behavior preservation notes: Windows default call paths are unchanged (same EXTRA_PATH entries, same LIBCLANG_PATH, same `maturin develop --release`); the error-message pointer moved from `src/antibot/BUILD_NOTES.md` (which doesn't exist at that path — pre-existing inaccuracy) to the README.

**Step 4: GREEN + suite** — `pytest tests/test_antibot_build_helper.py -q` (4 passed), then the full Windows offline gate.

**Step 5: Commit** — `git add tools/build_antibot.py tests/test_antibot_build_helper.py`
`git commit -m "feat(tools): cross-platform antibot build helper — injectable ALPS patch, per-OS venv paths"`

---

### Task 4: Linux build in the WSL2 sandbox — WSL2-gated, LIVE $0.00

**Objective:** Compile the engine on Ubuntu and pass the import sanity check — the P10 proof.

**Skip condition:** WSL2 absent per Task 2 ⇒ record `deferred: NO_LINUX_ENV (sandbox)` and skip to Task 6 (Docker may still exist).

**Steps (all inside `wsl -e bash -lc "..."`):**
1. Provision the sandbox (one-way rsync from the Windows repo — excludes heavy/irrelevant trees):
   ```bash
   mkdir -p ~/signals-linux
   rsync -a --delete \
     --exclude .git --exclude .venv --exclude data --exclude tmp --exclude PDFS \
     --exclude .zcode --exclude .hermes --exclude .pytest_cache --exclude .ruff_cache \
     --exclude signals.egg-info --exclude docs --exclude '*.pdf' \
     --exclude 'src/antibot/engine/target' \
     /mnt/c/Users/Jason/Documents/AI/Signals/ ~/signals-linux/
   ```
2. Toolchain (LIVE downloads, $0, 15-min hang guard; sudo inside WSL is fine):
   ```bash
   sudo apt-get update
   sudo apt-get install -y build-essential cmake ninja-build clang libclang-dev perl curl ca-certificates
   # Go 1.24+ tarball (apt golang-go on 24.04 is too old for BoringSSL's build):
   curl -fsSL https://go.dev/dl/go1.24.5.linux-amd64.tar.gz | sudo tar -C /usr/local -xz
   # Rust:
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
   ```
   (`go1.24.5` is a pinned example — if that URL 404s, pick the newest 1.24.x listed at go.dev/dl and record the substitution. Add `/usr/local/go/bin` and `~/.cargo/bin` to PATH in `~/.bashrc`.)
3. Sandbox venv + deps: `cd ~/signals-linux && python3.12 -m venv .venv && .venv/bin/pip install -e ".[rerank]" maturin`
4. Build through the helper (ALPS patch is applied automatically): `.venv/bin/python tools/build_antibot.py` — expected: `Patched ALPS codepoint` (or `already patched` on rebuild), 2–5 min first compile, then `engine_version:` + `tls:` sanity lines, exit 0. 30-min hang guard.
5. Record the exact outputs (patch line, compile time, engine_version, tls_library_version) for the README section in Task 6. Commit (Windows side, helper unchanged since Task 3 → this task's record rides with Task 5's commit).

---

### Task 5: Linux verification matrix — WSL2-gated, LIVE $0.00

**Objective:** The engine earns "ported" by passing its full verification stack on Linux.

**Steps (in `~/signals-linux`, inside WSL):**
1. Offline antibot suites: `.venv/bin/python -m pytest tests/test_antibot_engine.py tests/test_antibot_cookies.py tests/test_antibot_transport.py tests/test_antibot_ghost.py tests/test_antibot_routing.py tests/test_antibot_integration.py -q` — expected: all pass (engine-file tests now RUN instead of skip).
2. Fixture parity: included in `test_antibot_engine.py`; additionally run `.venv/bin/python -m pytest tests/test_antibot_parity.py -v -m antibot_live` (LIVE, tls.peet.ws, $0) — expected: JA4/JA3 parity green on Linux. A *network* failure (timeout/refused) is gate-4 territory, not a port failure; a *fingerprint diff* is a real finding — record the full diff output and STOP this task.
3. Full offline gate in the sandbox: `.venv/bin/python -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q` — expected: same pass count as the Windows gate (this re-proves the whole runtime on Linux with the engine present).
4. Write the **"Build (Linux/Ubuntu)"** section into `src/antibot/README.md` right after the Windows build section: the apt package list, the Go-tarball requirement (with version used), rustup, the helper invocation, the sandbox-rsync workflow, measured compile time, and the verification results (all three steps' outcomes). Self-contained — BUILD_NOTES.md is concurrently-owned and stays Windows-internal.
5. Windows offline gate (docs-only change — trivially green).
6. Commit: `git add src/antibot/README.md docs/plans/2026-10-31-antibot-engine-linux-port.md`
   `git commit -m "feat(antibot): engine verified on linux — ubuntu build recipe + parity results"`

---

### Task 6: Docker `engine` target — Docker-gated, Phase-0-dependent, LIVE $0.00

**Objective:** Bake the native engine into the image as a third target so containers get tier-1 parity instead of the curl_cffi fallback.

**Skip conditions:** Docker absent (Task 2 probe) ⇒ `deferred: DOCKER_ABSENT`; Phase 0's `Dockerfile` missing ⇒ `deferred: PHASE0_NOT_LANDED`.

**Step 1: Append to `Dockerfile`** (after the `browser` target):

```dockerfile
########## engine-builder: compiles the native signals_antibot wheel ##########
FROM python:3.12-slim-bookworm AS engine-builder
ENV PATH=/usr/local/go/bin:/root/.cargo/bin:${PATH}
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build clang libclang-dev perl curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://go.dev/dl/go1.24.5.linux-amd64.tar.gz | tar -C /usr/local -xz \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir maturin \
    && python -c "from tools.build_antibot import apply_registry_patches; apply_registry_patches()" \
    && cd src/antibot/engine && maturin build --release \
    && mv target/wheels/signals_antibot-*.whl /engine.whl

########## engine: browser + native antibot engine (tier-1 TLS parity) ##########
FROM browser AS engine
USER root
COPY --from=engine-builder /engine.whl /tmp/engine.whl
RUN pip install --no-cache-dir /tmp/engine.whl && rm /tmp/engine.whl
USER signals
```

Design notes: builder reuses the **same base** as the runtime targets (glibc-matched wheel); the patch runs via the helper's now-injectable function (no `tools` package change needed — implicit namespace import); the wheel is a build artifact only, never committed.

**Step 2: Build + verify** (LIVE downloads $0; 30-min hang guards):
1. `docker build --target engine -t signals:engine .` — expected: success; record image size (engine wheel + BoringSSL `.so` add roughly 30–60 MB over browser).
2. `docker run --rm signals:engine python -c "import signals_antibot as m; print(m.engine_version(), m.tls_library_version())"` — expected: version strings, exit 0.
3. `docker run --rm signals:engine doctor --no-network` — expected: the `antibot_engine` WARN is **gone** (engine present). Record before/after.
4. In-container antibot suites: `docker run --rm -w /app signals:engine python -m pytest tests/test_antibot_engine.py tests/test_antibot_transport.py tests/test_antibot_integration.py -q` — expected: green with the engine loaded.
5. Append the record (sizes, outputs, before/after doctor) to `docs/docker.md`.

**Step 3: Commit** — `git add Dockerfile docs/docker.md docs/plans/2026-10-31-antibot-engine-linux-port.md`
`git commit -m "feat(docker): engine target — native antibot baked into the image, doctor WARN cleared"`

---

### Task 7: TODO.md bookkeeping + Run Record (house format)

**Steps:**
1. TODO.md entry per house format (see the DELIVERED entry at TODO.md:12): `## Antibot engine Linux port — DELIVERED (2026-10-31)` (or PARTIAL/DEFERRED), `**Status: ...** — N commits across M tasks` with the provenance parenthetical (`overnight /goal run from docs/plans/2026-10-31-antibot-engine-linux-port.md; offline suite green at every batch gate`), a `- [x] **item** (hash)` checklist per task, deferrals (`NO_LINUX_ENV`/`DOCKER_ABSENT`/`TIMEOUT`/fingerprint-diff stop) with reasons, suite status at HEAD, and `Remaining fresh-decision territory:` — expected next decision: "add a nightly Linux engine-build lane to CI once the Docker engine target is verified, or proceed to Phase 1 VPS deployment". Update the top-of-file `Last reviewed:` line.
2. Fill this plan's Run Record table.
3. Windows offline gate one last time; `git diff TODO.md` foreign-edit check before staging (record, never revert).
4. Commit: `git add TODO.md docs/plans/2026-10-31-antibot-engine-linux-port.md`
   `git commit -m "docs(todo): antibot engine linux port record — run record filled"`

---

## Run Record (fill during execution)

| Task | Outcome | Commit | Notes |
| --- | --- | --- | --- |
| 1 | | | |
| 2 | | | envs present: WSL2? Docker? |
| 3 | | | |
| 4 | | | WSL2-gated |
| 5 | | | parity result: fixture / live |
| 6 | | | Docker+Phase-0-gated |
| 7 | | | |

## Risks / open questions

- **boring-sys 4.22 Linux build quirks:** the crate is widely built on Linux (it's its primary CI platform), so risk is shallow; the two known knobs are Go version (tarball 1.24.x, never apt) and libclang for bindgen (`libclang-dev`).
- **Fingerprint parity on Linux is expected to transfer byte-for-byte** (same vendored BoringSSL + same data table); if the fixture parity fails on Linux, that is a *finding* to record and stop on, not something to patch in this spike.
- **Ubuntu 24.04 assumption:** if the installed distro is 22.04, Python 3.12 needs deadsnakes — or defer to the Docker path, which carries its own 3.12.
- **Phase 0 dependency:** Task 6 edits the Phase 0 Dockerfile; if that plan hasn't landed, only Tasks 1–5 + 7 ship.
- **Out of scope (explicit):** CI lane for nightly Linux engine builds (natural follow-up, needs a working `engine` target first), any `src/` runtime changes, Windows build behavior changes, the ALPS patch content itself (upstream-tracked in BUILD_NOTES.md).

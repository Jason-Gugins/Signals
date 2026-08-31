# src/antibot — SignalsShadow

Native anti-bot fetch layer. A real BoringSSL engine (Rust + PyO3) whose TLS
fingerprint is byte-emergent from Chrome's own TLS machinery, an in-house
HTTP/2 stack, and temporal stealth — the behaviors bots fake and real browsers
just have.

## Philosophy: Chrome TLS, not Chrome-like

Most stealth HTTP clients fake the fingerprint: a hard-coded byte table of
ciphers and extensions shipped in a C library. Tables rot — Chrome moves a
codepoint, reorders an extension, and the table becomes the tell.

This module goes the other way: it drives **Chrome's actual engine**
(BoringSSL) with Chrome's actual settings. GREASE, extension permutation,
ECH-GREASE, ALPS, SCT, OCSP stapling, and brotli certificate compression are
*enabled features of the real engine*, not injected bytes. The ClientHello is
emergent, so it looks like Chrome because it *is* Chrome's handshake machinery.

**Update the data, not a C library.** When Chrome's posture shifts, edit the
data table at [`src/antibot/python/fingerprints.py`](python/fingerprints.py)
(config-of-record; the Rust side carries a compile-time mirror cross-checked
by tests). The same principle produced our one vendored BoringSSL patch — the
ALPS `application_settings` codepoint 17513 → 17613, auto-applied by
`tools/build_antibot.py` (see `BUILD_NOTES.md` at the repo root).

## Layers

| Layer | What it does | Why it matters |
|---|---|---|
| **Tier-1 TLS** | Real Chrome ClientHello from BoringSSL: GREASE, extension permutation (18 extensions), ECH-GREASE, ALPS (h2 settings 65536/0/6291456/262144, codepoint 17613), SCT, OCSP, brotli cert-compression, Chrome's sig-alg and key-share order | JA4/JA3 match a real Chrome — fixture-asserted (`chrome_fingerprint_reference.json`), live-opt-in via `tls.peet.ws` |
| **Own HTTP/2** | In-house h2 stack: HPACK per RFC 7541 (all 257 Huffman symbols), Chrome-exact SETTINGS/WINDOW_UPDATE frames, pseudo-header order `m,a,s,p` | Akamai h2 SETTINGS matched against a real-Chrome capture (frame-level, fixture-verified); the layer curl_cffi and httpx get wrong |
| **Temporal stealth** | TLS session resumption, connection pooling, conditional revalidation (`ETag`/`Last-Modified` → `If-None-Match` → serve cached body on 304) | "The tell nobody else fakes" — bots hit every URL fresh; browsers re-validate from cache |
| **Happy Eyeballs** | IPv6/IPv4 address-family split with a 250ms staggered dial race (`eyeballs.rs`; `split_families` / `connect_eyeballs_timing` PyO3 exports) | Slow or broken IPv6 paths never stall the fetch — real browsers race families, most clients don't |
| **Solve-and-bounce** | A **headed** browser (Patchright) only solves JS challenges and harvests clearance cookies; the TLS tier then fetches the content. Forced headed — DataDome flags headless Chromium (`ghost.py` overrides the config) | Browser minutes are expensive and slow; the fast tier does the bulk fetching |
| **Self-improving routing** | Per-domain state machine `Warm / Cold / SkipToSolve / RecheckCold`; learns each domain's clearance-cookie lifetime and skips re-solving while cookies live | Fewer solves over time; state persists in `data/antibot/routing.json` |
| **Persistent cookie jar** | `PersistentCookieJar` accumulates `Set-Cookie` state across fetches (caller-wins merge), persisted to `data/antibot/cookies.json` | Browsers keep cookies across requests; per-fetch cookie dropping is another tell |

## Layout

```
src/antibot/
├── engine/                  # Rust crate `signals_antibot` (PyO3)
│   └── src/
│       ├── tls.rs           # BoringSSL context — Chrome posture (mirror of fingerprints.py)
│       ├── h2/              # Own HTTP/2: hpack.rs, frames.rs, flow.rs, tables.rs
│       ├── session.rs       # TLS session store (resumption)
│       ├── pool.rs          # Connection pool
│       ├── eyeballs.rs      # Happy Eyeballs: v4/v6 split + 250ms stagger race
│       └── lib.rs           # PyO3 bridge
└── python/
    ├── transport.py         # SignalsTransport: native fetch, 304 cache, cookie-jar merge, curl_cffi fallback
    ├── ghost.py             # PatchrightGhost (forced headed) + solve_and_bounce orchestration
    ├── routing.py           # RouteState: Warm/Cold/SkipToSolve/RecheckCold + lifetime learning
    ├── temporal.py          # RevalidationCache (validators, 304 serving)
    ├── cookies.py           # PersistentCookieJar — persists to data/antibot/cookies.json
    └── fingerprints.py      # Chrome data table — config-of-record
```

## Build (Windows)

Prerequisites: **Rust**, **Go**, **NASM**, **CMake**, **Ninja**, **LLVM/Clang**, **MSVC Build Tools**, and `pip install maturin` into the venv (Go + NASM compile BoringSSL; Clang supplies bindgen headers; Ninja is the CMake generator).

**The toolchain paths in the helpers are machine-specific** — edit `EXTRA_PATH` in `tools/build_antibot.py` (or the PATH lines in `build_antibot_env.bat`) for your machine.

Build into the venv:

```powershell
# Preferred: the bat wrapper — sets up the MSVC environment (vcvars64) itself.
cmd /c "C:\path\to\Signals\tools\build_antibot_env.bat"

# Or the Python helper — only from a VS dev prompt (cl.exe/link.exe on PATH);
# from a fresh shell it fails with linker errors because it never calls vcvars.
.venv\Scripts\python.exe tools\build_antibot.py
```

In git-bash, prefix builds with `export MSYS2_ARG_CONV_EXCL='*'` (maturin's
arguments get path-mangled otherwise).

Notes:

- The first build compiles BoringSSL — expect **2–5 minutes** depending on disk; it's cached afterward (Rust-only changes rebuild fast).
- `tools/build_antibot.py` **auto-applies the ALPS codepoint patch** (17513 → 17613) to the vendored BoringSSL on **every build, idempotently** — no manual step. Just always build through the helper; building outside it after a cargo-registry refresh silently loses the patch (details in `BUILD_NOTES.md`).
- **The engine is optional at runtime.** If it isn't built, `SignalsTransport` degrades gracefully to a `curl_cffi` fallback — the pipeline keeps working, minus tier-1 parity. `engine_available` tells you which path you're on.

### Engine vs fallback — what survives

| Capability | Engine built | curl_cffi fallback |
|---|---|---|
| Chrome ClientHello (JA4 parity) | yes | no (impersonation table) |
| Own HTTP/2 (Akamai SETTINGS) | yes | no (library h2) |
| TLS resumption / pooling | yes | partial (library defaults) |
| 304 revalidation, cookie jar, ghost, routing | yes | yes (pure Python) |

## Verification

```powershell
# Always-on suites (engine config, HPACK round-trip, cookies, routing, ghost, integration):
.\.venv\Scripts\python.exe -m pytest tests/test_antibot_engine.py tests/test_antibot_cookies.py tests/test_antibot_transport.py tests/test_antibot_ghost.py tests/test_antibot_routing.py tests/test_antibot_integration.py -q
# (the engine file's 3 live tests auto-skip without -m antibot_live; expect ~65 passing)

# Live parity (opt-in, needs network — hits tls.peet.ws):
.\.venv\Scripts\python.exe -m pytest tests/test_antibot_parity.py -v -m antibot_live
```

The parity suite compares the engine's fingerprint against a ground-truth
capture from a real headed Chromium:
`tests/fixtures/antibot/chrome_fingerprint_reference.json`. When Chrome
updates and parity drifts, re-capture the reference:

```powershell
.\.venv\Scripts\python.exe tools\capture_chrome_fingerprint.py
```

**Padding-extension tolerance:** Chrome *optionally* emits a TLS padding
extension depending on ClientHello size (a real Chrome quirk, not a bug). The
parity assertions tolerate its presence/absence rather than pinning it.

## Honest limits

Stated plainly, DonSeTch-style:

- **No CAPTCHA solving.** Interactive challenges (hCaptcha, reCAPTCHA image
  grids) are an honest dead end — the module stops and reports, it doesn't
  pretend.
- **Login-required sites are out of scope.** No auth bypass, no session
  impersonation.
- **Detection is never guaranteed.** Behavioral ML (mouse telemetry, request
  entropy) and Proof-of-Browser style challenges still exist. Solve-and-bounce
  covers those by routing hard targets through a real browser.
- **BoringSSL tracks Chrome.** Where BoringSSL lacks a Chrome capability, we
  wait for upstream: Chrome 151's post-quantum signature algorithms (ML-DSA)
  fall back to the classic list until BoringSSL ships ML-DSA. ECH-GREASE and
  ALPS are per-connection FFI (no context-level API). The full API-gap ledger
  lives in `BUILD_NOTES.md`.
- **Fingerprint parity ≠ clearance.** The Aug 2026 live finding: DataDome
  clearance cookies are **fingerprint-bound to the browser JS session that
  solved them** — replaying a harvested cookie through this engine's
  byte-identical Chrome TLS+h2 still returns the 403 interstitial, because
  DataDome validates the JS probe behind the cookie, not just network
  identity. Consequence: hard targets must render *inside* the browser (that
  is exactly what the G2 runner does); the TLS tier clears moderately
  protected targets and everything behind plain Cloudflare. Empirical detail:
  `src/sources/marketplace/README.md` → "Empirical findings".

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `LNK…` linker errors on build | MSVC env not set up | Use `build_antibot_env.bat` (calls vcvars64), or run from a VS dev prompt |
| Maturin args look mangled in git-bash | MSYS path conversion | `export MSYS2_ARG_CONV_EXCL='*'` before building |
| bindgen: `libclang not found` | LLVM location | Set `LIBCLANG_PATH=C:\Program Files\LLVM\bin` (both helpers do this) |
| Parity tests fail with stale ALPS codepoint | Built outside the helper after a registry refresh | Rebuild via `tools/build_antibot.py` (re-applies the patch); `touch Cargo.toml` to invalidate the boring-sys cache |
| bindgen/Go/NASM not found | Machine-specific paths | Edit `EXTRA_PATH` in `build_antibot.py` / PATH in the bat |

## Configuration

The `antibot:` block in `config/default.yaml` gates the module:

```yaml
antibot:
  enabled: true          # wire SignalsShadow into the bypass waterfalls
  fallback: curl_cffi    # tier used when the engine isn't built
  recheck_after_hours: 24
```

`config.antibot.enabled=true` makes the orchestrator construct a
`SignalsTransport` and hand it to `DataDomeBypass`/`CloudflareBypass` as the
preferred tier-1 (tier order: cookie reuse → signals_shadow → curl_cffi →
stealth browser → solver → hard stop). Construction is guarded — an unbuilt
engine degrades gracefully to the fallback.

The transport itself is parameterized at construction: `user_agent`, `proxy`,
and `conditional` (304 revalidation on/off). Routing state persists at
`data/antibot/routing.json` (configurable `path` + `recheck_seconds` on
`RouteState`).

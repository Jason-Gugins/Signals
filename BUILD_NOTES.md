# BUILD_NOTES.md — native antibot engine (signals_antibot)

Building and API-gap notes for `src/antibot/engine` (crate `signals_antibot`:
boring/boring-sys 4.22.0 + PyO3 0.22.6, real BoringSSL).

## Build (Windows)

- Build MUST run inside the MSVC env: `cmd /c "C:\Users\Jason\Documents\AI\Signals\tools\build_antibot_env.bat"`
  from `src/antibot/engine` (helper sets vcvars64, Go/LLVM/NASM PATH, LIBCLANG_PATH, Ninja, then `maturin develop --release`).
- In git-bash, prefix with `export MSYS2_ARG_CONV_EXCL='*'`.
- First build compiles BoringSSL (~2–4 min); cached afterwards. Rust-only errors rebuild fast.

## Chrome posture (Task 1)

The Chrome ClientHello configuration lives in `src/antibot/engine/src/tls.rs`
(compile-time mirror) with `src/antibot/python/fingerprints.py` as the
config-of-record data table. Config-level gate: `tests/test_antibot_engine.py::test_tls_config_has_chrome_behaviors`.
Live parity (skipped by default):
`.venv/Scripts/python.exe -m pytest tests/test_antibot_parity.py -v -m antibot_live`
reference captured from real Chromium via `tools/capture_chrome_fingerprint.py`.

## Honest API-gap ledger (boring 4.22.0)

- **GREASE** — `SslContextBuilder::set_grease_enabled(true)` exists. ✅ enabled at context level.
- **Extension permutation** — `SslContextBuilder::set_permute_extensions(true)` exists. ✅ enabled at context level.
- **SCT** — `SslContextBuilder::enable_signed_cert_timestamps()`. ✅ context level.
- **OCSP stapling** — `SslContextBuilder::enable_ocsp_stapling()`. ✅ context level.
- **Cert compression (brotli)** — `SslContextBuilder::add_certificate_compression_algorithm`
  with a custom `CertificateCompressor` (brotli crate); algorithm id =
  `CertificateCompressionAlgorithm::BROTLI`. ✅ context level.
- **ECH-GREASE** — boring exposes it **only per-connection**
  (`SslRef::set_enable_ech_grease`, FFI `SSL_set_enable_ech_grease`); there is no
  `SSL_CTX`-level toggle (upstream BoringSSL doesn't have one either — Chrome sets it
  per handshake). The transport calls it on every `ConnectConfiguration` before the
  handshake, so every connection carries it; `tls_config_json()` reports `ech_grease: true`
  on that basis.
- **ALPS** — same story: FFI-only `SSL_add_application_settings(ssl, "h2", settings)`
  (no safe wrapper in boring; we call `boring_sys::ffi` directly via
  `ForeignTypeRef::as_ptr()`). Applied per-connection in the transport for h2 with
  Chrome's exact SETTINGS values (65536 / 0 / 6291456 / 262144).
  `tls_config_json()` reports `alps: true` on the same basis.
- **Signature algorithms** — no safe wrapper for `SSL_CTX_set_signing_algorithm_prefs`;
  called via raw FFI with Chrome's 8-algorithm offer order.
- **Key-share groups** — `set_curves_list` with `X25519MLKEM768:X25519:P-256:P-384`
  (falls back to the classic set if the linked BoringSSL lacks hybrid ML-KEM).
- **Version string** — boring 4.22 does not export `openssl_version()`; we read the
  bindgen constant `boring_sys::OPENSSL_VERSION_TEXT` instead.

## Known non-parity (documented, not hidden)

- `probe_fingerprint` speaks a **minimal** HTTP/2 request (literal HPACK, fixed
  preface) so the TLS layer can be validated against tls.peet.ws before Task 2.
  The *server-echoed TLS* fields (ja4, ja3_hash, tls_extensions) are independent of
  request-side h2 bytes; `akamai_h2` from the probe is therefore NOT Chrome-exact
  until Task 2's own h2 stack (Chrome-exact SETTINGS/WINDOW_UPDATE/HPACK) lands.

## Vendored BoringSSL patch (ALPS codepoint)

- **`TLSEXT_TYPE_application_settings`: 17513 → 17613.** Real Chrome 151+ emits
  the final codepoint 17613 (0x44CD); upstream BoringSSL 4.22.0 still hardcodes
  the legacy draft value 17513 (0x4469). Live parity vs real Chromium showed
  `application_settings_old (17513)` from our engine vs `application_settings (17613)`
  from Chrome — the only extension-set diff out of 18.
- Patch location (recreate after `cargo clean` if the registry source is
  refreshed): `~/.cargo/registry/src/*/boring-sys-4.22.0/deps/boringssl/src/include/openssl/tls1.h`
  — change the `#define TLSEXT_TYPE_application_settings` value and touch
  `Cargo.toml` to invalidate the boring-sys build cache.
- This is the "update the data, not a C library" principle applied to the one
  constant Chrome moved; the alternative (ALPS with a stale codepoint) is a
  detectable fingerprint mismatch.

## Signature-algorithm fallback

- Chrome 151's sig-alg list leads with hybrid PQ (ML-DSA 0x0904/0x0905/0x0906).
  BoringSSL without ML-DSA rejects the whole list (`[INVALID_SIGNATURE_ALGORITHM]`),
  so `tls.rs` falls back to the classic (non-PQ) prefix when the full list fails.
  Parity impact: none for JA4 prefix; PQ sig-algs will be offered once upstream
  BoringSSL ships ML-DSA (DonSeTch documents the same gap).

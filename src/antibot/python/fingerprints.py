"""Pinned Chrome TLS posture — the config-of-record data table.

Principle: when Chrome's ClientHello posture shifts, you update THIS table —
not a C library. The native engine (src/antibot/engine/src/tls.rs) applies
these values to the real BoringSSL SslContext; the ClientHello is emergent
from Chrome's own TLS machinery (GREASE, extension permutation, ECH-GREASE,
ALPS, SCT, OCSP, brotli cert-compression), not a faked byte table.

The Rust module carries a compile-time mirror of this table (it cannot read
Python). tests/test_antibot_engine.py cross-checks the two stay in sync for
the fields both sides know (ALPN, counts, ALPS settings).

Parity ground truth: tests/fixtures/antibot/chrome_fingerprint_reference.json
captured from real Chromium via tools/capture_chrome_fingerprint.py.
"""

from __future__ import annotations

CHROME_TARGET: dict = {
    # Pinned Chrome version this posture was captured from
    # (matches user_agent in tests/fixtures/antibot/chrome_fingerprint_reference.json).
    "major": 151,
    "alpn": ["h2", "http/1.1"],
    "cert_compression": ["brotli"],
    # TLS 1.3 cipher suites, offer order (JA4-visible).
    "ciphers_tls13": [
        "TLS_AES_128_GCM_SHA256",
        "TLS_AES_256_GCM_SHA384",
        "TLS_CHACHA20_POLY1305_SHA256",
    ],
    # TLS 1.2 cipher suites, offer order (JA4-visible).
    "ciphers_tls12": [
        "ECDHE-ECDSA-AES128-GCM-SHA256",
        "ECDHE-RSA-AES128-GCM-SHA256",
        "ECDHE-ECDSA-AES256-GCM-SHA384",
        "ECDHE-RSA-AES256-GCM-SHA384",
        "ECDHE-ECDSA-CHACHA20-POLY1305",
        "ECDHE-RSA-CHACHA20-POLY1305",
        "ECDHE-RSA-AES128-SHA",
        "ECDHE-RSA-AES256-SHA",
        "AES128-GCM-SHA256",
        "AES256-GCM-SHA384",
        "AES128-SHA",
        "AES256-SHA",
    ],
    # Key-share groups, offer order (hybrid PQ first, as Chrome ships).
    "curves": ["X25519MLKEM768", "X25519", "P-256", "P-384"],
    # Signature algorithms, offer order (Chrome 151: hybrid PQ first).
    "signature_algorithms": [
        "0x0904", "0x0905", "0x0906",           # mldsa44/65/87
        "ecdsa_secp256r1_sha256", "rsa_pss_rsae_sha256", "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384", "rsa_pss_rsae_sha384", "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512", "rsa_pkcs1_sha512",
    ],
    # Native ClientHello behaviors enabled in the engine.
    "grease_enabled": True,           # RFC 8701
    "extension_permutation": True,    # Chrome's per-handshake extension shuffle
    "ech_grease": True,               # encrypted_client_hello GREASE (per-connection in BoringSSL)
    "alps": True,                     # application settings for h2 (per-connection in BoringSSL)
    "sct": True,                      # signed_certificate_timestamp
    "ocsp_stapling": True,            # status_request
    # Chrome's h2 SETTINGS — also sent in the ALPS extension (placeholder until
    # the Task-2 own-h2 stack consumes the wire bytes directly).
    "h2_settings": {
        "HEADER_TABLE_SIZE": 65536,
        "ENABLE_PUSH": 0,
        "INITIAL_WINDOW_SIZE": 6291456,
        "MAX_HEADER_LIST_SIZE": 262144,
    },
}

__all__ = ["CHROME_TARGET"]

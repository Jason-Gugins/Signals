"""Task 0 spike tests: native signals_antibot engine (real BoringSSL via boring crate + PyO3).
Task 1: Chrome-configured TLS context assertions (config-level, always-on)."""

import json

import pytest

from src.antibot.python.fingerprints import CHROME_TARGET


def test_native_engine_importable():
    import signals_antibot

    assert hasattr(signals_antibot, "engine_version")
    assert isinstance(signals_antibot.engine_version(), str)


def test_native_engine_tls_sha256():
    """Prove the engine is linked to real BoringSSL (not system OpenSSL):
    expose BoringSSL_version() and check it reports a BoringSSL string."""
    import signals_antibot

    v = signals_antibot.tls_library_version()
    assert "BoringSSL" in v


def test_tls_config_has_chrome_behaviors():
    """The SslContext must be configured with Chrome's native ClientHello behaviors."""
    import signals_antibot

    cfg = json.loads(signals_antibot.tls_config_json())
    assert cfg["grease_enabled"] is True
    assert cfg["extension_permutation"] is True
    assert cfg["ech_grease"] is True          # per-connection toggle; see BUILD_NOTES.md
    assert cfg["alps"] is True                # per-connection toggle; see BUILD_NOTES.md
    assert cfg["brotli_cert_compression"] is True
    assert cfg["sct"] is True
    assert cfg["ocsp_stapling"] is True
    assert "h2" in cfg["alpn"] and "http/1.1" in cfg["alpn"]
    assert cfg["tls_max"] == "TLS1.3" and cfg["tls_min"] == "TLS1.2"


def test_tls_config_matches_fingerprints_table():
    """The engine's config must match the pinned Chrome data table (config-of-record)."""
    import signals_antibot

    cfg = json.loads(signals_antibot.tls_config_json())
    assert cfg["alpn"] == CHROME_TARGET["alpn"]
    assert cfg["ciphers_tls13_count"] == len(CHROME_TARGET["ciphers_tls13"])
    assert cfg["ciphers_tls12_count"] == len(CHROME_TARGET["ciphers_tls12"])
    assert cfg["sig_algs_count"] == len(CHROME_TARGET["signature_algorithms"])
    alps = {(s["id"], s["value"]) for s in cfg["alps_h2_settings"]}
    expected = {v for v in CHROME_TARGET["h2_settings"].items()}
    # h2_settings keys are strings in the table; ids are ints on the wire — compare values
    assert {v for _, v in alps} == {v for _, v in expected}
    assert cfg["curves"] == ":".join(CHROME_TARGET["curves"])


def test_probe_fingerprint_returns_endpoint_json():
    """probe_fingerprint must return the fingerprint endpoint's echoed data.
    (Network call goes through the native engine, not httpx, so the autouse
    no-network guard does not apply; this validates shape only.)"""
    import signals_antibot

    fp = json.loads(signals_antibot.probe_fingerprint("https://tls.peet.ws/api/all"))
    assert fp["ja4"] is not None and isinstance(fp["ja4"], str)
    assert fp["ja4"].startswith("t13")
    assert isinstance(fp["tls_extensions"], list) and fp["tls_extensions"]


# ---------------------------------------------------------------------------
# Task 3: temporal stealth — TLS resumption, h2 pooling
# ---------------------------------------------------------------------------

def test_signals_engine_class_present():
    import signals_antibot

    engine = signals_antibot.SignalsEngine()
    assert engine.pool_size() == 0
    assert engine.session_count() == 0
    engine.close()
    engine.close_pool()


@pytest.mark.antibot_live
def test_engine_second_fetch_reuses_connection_or_resumes_session():
    """Temporal stealth, live: two fetches of the same origin through ONE
    SignalsEngine must show browser-like temporal behavior — the second
    response is served over the pooled h2 connection (reused_connection)
    and/or after an abbreviated TLS handshake (resumed_session)."""
    import signals_antibot

    engine = signals_antibot.SignalsEngine()
    r1 = json.loads(engine.fetch("https://www.cloudflare.com/cdn-cgi/trace"))
    r2 = json.loads(engine.fetch("https://www.cloudflare.com/cdn-cgi/trace"))
    engine.close()
    assert r1["status"] == 200
    assert r2["status"] == 200
    assert r2["reused_connection"] is True or r2["resumed_session"] is True


@pytest.mark.antibot_live
def test_engine_tls_session_resumption_abbreviates_handshake():
    """With the pool dropped but session tickets kept, the next dial must
    resume the TLS session (abbreviated handshake) on an origin that honors
    resumption tickets."""
    import signals_antibot

    engine = signals_antibot.SignalsEngine()
    r1 = json.loads(engine.fetch("https://httpbin.org/get"))
    assert engine.session_count() >= 1
    engine.close_pool()
    r2 = json.loads(engine.fetch("https://httpbin.org/get"))
    engine.close()
    assert r1["status"] == 200 and r2["status"] == 200
    assert r2["resumed_session"] is True


# ---------------------------------------------------------------------------
# Happy Eyeballs: IPv6/IPv4 race with 250ms stagger (temporal stealth)
# ---------------------------------------------------------------------------

def test_split_families_partitions_dual_stack_host():
    """split_families resolves the host and reports the v4/v6 partition
    counts (pure resolution logic — no connection)."""
    import signals_antibot

    out = json.loads(signals_antibot.split_families("cloudflare.com"))
    # cloudflare.com is dual-stack: both families must be present.
    assert out["v4_addrs"] >= 1
    assert out["v6_addrs"] >= 1


def test_split_families_single_family_host():
    """A v4-only hostname resolves to v4 only (partition correctness)."""
    import signals_antibot

    out = json.loads(signals_antibot.split_families("ipv4only.arpa"))
    assert out["v4_addrs"] >= 1 and out["v6_addrs"] == 0


@pytest.mark.antibot_live
def test_connect_eyeballs_timing_reports_winner():
    """connect_eyeballs_timing dials cloudflare.com with the Happy Eyeballs
    race and reports which family won."""
    import signals_antibot

    out = json.loads(signals_antibot.connect_eyeballs_timing("cloudflare.com"))
    assert out["stagger_ms"] == 250
    assert out["winner_family"] in ("v4", "v6")
    assert out["v4_addrs"] >= 1

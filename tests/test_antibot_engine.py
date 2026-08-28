"""Task 0 spike tests: native signals_antibot engine (real BoringSSL via boring crate + PyO3)."""


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

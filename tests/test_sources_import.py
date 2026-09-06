# Sources with no registered SourceAdapter: community_reddit is a disabled
# stub; gkg_ids (plan T3b) is a resolve-time identity task label that only
# carries a rate_per_host override (consumed by _source_rate_overrides).
OPTIONAL_UNREGISTERED = {"community_reddit", "gkg_ids"}


def test_all_yaml_keys_are_registered():
    import src.sources  # noqa: F401
    from src.core.config import Config
    from src.sources.registry import SOURCES

    cfg = Config()
    cfg.config_dir = "config"
    names = set((cfg.load_yaml("sources").get("sources") or {}))
    missing = names - set(SOURCES) - OPTIONAL_UNREGISTERED
    assert not missing, missing

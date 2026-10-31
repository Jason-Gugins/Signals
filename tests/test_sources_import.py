# Sources with no registered SourceAdapter: gkg_ids (plan T3b) is a
# resolve-time identity task label that only carries a rate_per_host override
# (consumed by _source_rate_overrides). community_reddit IS registered
# (src/sources/community/reddit.py) and is enabled via the scoped /search.rss
# robots override (operator-approved 2026-10-30) — so it must stay registered.
OPTIONAL_UNREGISTERED = {"gkg_ids"}


def test_all_yaml_keys_are_registered():
    import src.sources  # noqa: F401
    from src.core.config import Config
    from src.sources.registry import SOURCES

    cfg = Config()
    cfg.config_dir = "config"
    names = set((cfg.load_yaml("sources").get("sources") or {}))
    missing = names - set(SOURCES) - OPTIONAL_UNREGISTERED
    assert not missing, missing

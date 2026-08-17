OPTIONAL_UNREGISTERED = {"community_reddit"}


def test_all_yaml_keys_are_registered():
    import src.sources  # noqa: F401
    from src.core.config import Config
    from src.sources.registry import SOURCES

    cfg = Config()
    cfg.config_dir = "config"
    names = set((cfg.load_yaml("sources").get("sources") or {}))
    missing = names - set(SOURCES) - OPTIONAL_UNREGISTERED
    assert not missing, missing

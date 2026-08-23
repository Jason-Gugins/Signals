"""Tests for the YAML + env + CLI config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.config import Config, ConfigError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = ROOT / "config" / "default.yaml"


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_defaults_load(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")

    cfg = Config.load(yaml_path=DEFAULT_YAML, env_path=env_path)

    assert cfg.http.timeout_seconds == 30
    assert cfg.http.max_workers == 6
    assert cfg.browser.enabled is False
    assert cfg.storage.db_path == "data/signals.db"
    assert cfg.pipeline.default_lookback_days == 365
    assert cfg.contact_email is None


def test_nested_yaml_override_applies(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)
    yaml_path = _write_yaml(
        tmp_path / "override.yaml",
        {
            "http": {"timeout_seconds": 12, "max_workers": 2},
            "browser": {"enabled": True, "min_delay": 1.0},
            "storage": {"db_path": "tmp/alt.db"},
        },
    )
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")

    cfg = Config.load(yaml_path=yaml_path, env_path=env_path)

    assert cfg.http.timeout_seconds == 12
    assert cfg.http.max_workers == 2
    assert cfg.browser.enabled is True
    assert cfg.browser.min_delay == 1.0
    assert cfg.storage.db_path == "tmp/alt.db"
    # unspecified nested fields keep dataclass defaults
    assert cfg.http.verify_tls is True


def test_env_var_beats_yaml(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path / "base.yaml",
        {"storage": {"db_path": "from-yaml.db"}},
    )
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SIGNALS_DB_PATH", "from-env.db")
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "ops@example.com")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")

    cfg = Config.load(yaml_path=yaml_path, env_path=env_path)

    assert cfg.storage.db_path == "from-env.db"
    assert cfg.contact_email == "ops@example.com"
    assert cfg.github_token == "gh-test"
    assert cfg.alert_webhook_url == "https://hooks.example/x"


def test_cli_override_beats_env(tmp_path, monkeypatch):
    yaml_path = _write_yaml(
        tmp_path / "base.yaml",
        {"storage": {"db_path": "from-yaml.db"}},
    )
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SIGNALS_DB_PATH", "from-env.db")
    monkeypatch.setenv("SIGNALS_CONTACT_EMAIL", "env@example.com")

    cfg = Config.load(
        yaml_path=yaml_path,
        env_path=env_path,
        overrides={
            "storage": {"db_path": "from-cli.db"},
            "contact_email": "cli@example.com",
        },
    )

    assert cfg.storage.db_path == "from-cli.db"
    assert cfg.contact_email == "cli@example.com"


def test_resolved_user_agent_injects_email(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    cfg = Config.load(yaml_path=DEFAULT_YAML, env_path=env_path)
    cfg.contact_email = "rep@acme.com"

    ua = cfg.resolved_user_agent()

    assert "rep@acme.com" in ua
    assert "REPLACE_ME" not in ua


def test_resolved_user_agent_raises_when_placeholder_unresolved(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    cfg = Config.load(yaml_path=DEFAULT_YAML, env_path=env_path)

    assert cfg.contact_email is None
    assert "REPLACE_ME" in cfg.http.user_agent
    with pytest.raises(ConfigError):
        cfg.resolved_user_agent()


def test_load_yaml_signals_has_39_types():
    cfg = Config()
    cfg.config_dir = str(ROOT / "config")
    data = cfg.load_yaml("signals")
    assert len(data["types"]) == 39
    # cache: same object on second load
    assert cfg.load_yaml("signals") is data


def test_cloudflare_config_defaults():
    cfg = Config()
    assert cfg.cloudflare.enabled is True
    assert cfg.cloudflare.bypass_strategy == "browser_first"
    assert cfg.cloudflare.solve_timeout_ms == 20000
    assert cfg.cloudflare.cookie_ttl_hours == 24
    assert cfg.cloudflare.solver_api_key is None
    assert cfg.cloudflare.solver_provider is None
    assert cfg.cloudflare.headed_fallback is False
    assert cfg.cloudflare.headed_solve_timeout_ms == 120000


def test_cloudflare_config_from_yaml(tmp_path):
    yaml = tmp_path / "default.yaml"
    yaml.write_text(
        "cloudflare:\n"
        "  enabled: true\n"
        "  bypass_strategy: solver_first\n"
        "  solve_timeout_ms: 30000\n"
        "  solver_provider: 2captcha\n"
        "  solver_api_key: secret\n",
        encoding="utf-8",
    )
    # env_path prevents real .env from polluting the test (existing convention)
    cfg = Config.load(yaml_path=yaml, env_path=tmp_path / "empty.env")
    assert cfg.cloudflare.bypass_strategy == "solver_first"
    assert cfg.cloudflare.solver_api_key == "secret"
    assert cfg.cloudflare.solver_provider == "2captcha"


def test_cloudflare_config_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_SOLVER_API_KEY", "envkey")
    monkeypatch.setenv("CLOUDFLARE_SOLVER_PROVIDER", "anticaptcha")
    monkeypatch.setenv("CLOUDFLARE_BYPASS_STRATEGY", "browser_only")
    monkeypatch.setenv("CLOUDFLARE_HEADED_FALLBACK", "true")
    cfg = Config.load(env_path=tmp_path / "empty.env")
    assert cfg.cloudflare.solver_api_key == "envkey"
    assert cfg.cloudflare.solver_provider == "anticaptcha"
    assert cfg.cloudflare.bypass_strategy == "browser_only"
    assert cfg.cloudflare.headed_fallback is True

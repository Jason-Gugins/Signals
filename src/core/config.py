"""Config loader — YAML + env vars + CLI overrides.

Dataclass-based config with nested sections matching config/default.yaml.
`_apply_dict_to_config` is copied from ../Linkedin/src/config.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Invalid or incomplete runtime configuration."""


@dataclass
class HttpConfig:
    user_agent: str = "SignalsResearchBot/0.1 (+contact: REPLACE_ME@example.com)"
    timeout_seconds: int = 30
    max_retries: int = 3
    backoff_base: float = 1.5
    respect_robots: bool = True
    # host + path_prefix pairs that skip robots.txt (scoped; not a global off switch)
    robots_allow: list = field(
        default_factory=lambda: [{"host": "news.google.com", "path_prefix": "/rss/"}]
    )
    max_workers: int = 6
    default_rate_per_host: float = 1.0
    verify_tls: bool = True


@dataclass
class BrowserConfig:
    enabled: bool = False
    headless: bool = True
    viewport: dict = field(default_factory=lambda: {"width": 1920, "height": 1080})
    locale: str = "en-US"
    timezone: str = "America/Toronto"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    proxy_server: Optional[str] = None
    min_delay: float = 5.0
    max_delay: float = 15.0
    session_dir: str = "data/state"


@dataclass
class CloudflareConfig:
    enabled: bool = True
    # tier order: "browser_first" (solve via Chromium, then solver, then headed),
    #             "solver_first" (external API, then browser), "browser_only", "disabled"
    bypass_strategy: str = "browser_first"
    solve_timeout_ms: int = 20000
    # ceiling only — real cookie `expires` is the source of truth; use min(real, now+ttl)
    cookie_ttl_hours: int = 24
    headed_fallback: bool = False
    headed_solve_timeout_ms: int = 120000  # hard cap for ad-hoc headed runs
    solver_provider: Optional[str] = None  # "2captcha" | "anticaptcha" | None
    solver_api_key: Optional[str] = None
    min_retry_delay_s: float = 2.0
    max_retry_delay_s: float = 8.0
    max_solves_per_domain_per_24h: int = 1  # anti-escalation cap


@dataclass
class StorageConfig:
    db_path: str = "data/signals.db"
    raw_dir: str = "data/raw"
    export_dir: str = "data/exports"
    briefs_dir: str = "data/briefs"
    alerts_dir: str = "data/alerts"
    recon_dir: str = "data/recon"
    keep_raw_days: int = 400


@dataclass
class ExternalDbConfig:
    linkedin_db: str = "../Linkedin/data/linkedin.db"
    repvue_db: str = "../repvue-scraper/data/repvue.db"
    linkedin_cli_cwd: str = "../Linkedin"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/logs/signals.log"
    rotation: str = "10 MB"


@dataclass
class PipelineConfig:
    default_lookback_days: int = 365
    score_on_collect: bool = True
    alert_on_new_primary: bool = True


@dataclass
class Config:
    http: HttpConfig = field(default_factory=HttpConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    cloudflare: CloudflareConfig = field(default_factory=CloudflareConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    external_dbs: ExternalDbConfig = field(default_factory=ExternalDbConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    contact_email: Optional[str] = None
    github_token: Optional[str] = None
    alert_webhook_url: Optional[str] = None
    config_dir: str = "config"

    _yaml_cache: dict[str, dict] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def load(
        cls,
        yaml_path: str | Path | None = None,
        env_path: str | Path | None = None,
        overrides: dict | None = None,
    ) -> Config:
        """Load config from YAML, then .env, then CLI overrides."""
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        data: dict[str, Any] = {}
        if yaml_path is None:
            default_path = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
            if default_path.exists():
                yaml_path = default_path
        if yaml_path and Path(yaml_path).exists():
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        config = cls()
        if yaml_path:
            config.config_dir = str(Path(yaml_path).parent)
        _apply_dict_to_config(config, data)
        _apply_env_overrides(config)
        if overrides:
            _apply_dict_to_config(config, overrides)
        return config

    def resolved_user_agent(self) -> str:
        """Substitutes contact_email into the UA. Raises if UA still says REPLACE_ME."""
        ua = self.http.user_agent
        if self.contact_email:
            ua = ua.replace("REPLACE_ME@example.com", self.contact_email)
            ua = ua.replace("REPLACE_ME", self.contact_email)
        if "REPLACE_ME" in ua:
            raise ConfigError(
                "User-Agent still contains REPLACE_ME; set SIGNALS_CONTACT_EMAIL"
            )
        return ua

    def load_yaml(self, name: str) -> dict:
        """Loads config/<name>.yaml with a small in-process cache."""
        key = name if name.endswith(".yaml") else f"{name}.yaml"
        if key not in self._yaml_cache:
            path = Path(self.config_dir) / key
            with open(path, encoding="utf-8") as f:
                self._yaml_cache[key] = yaml.safe_load(f) or {}
        return self._yaml_cache[key]


def _apply_dict_to_config(config: Config, data: dict) -> None:
    """Recursively apply a dict to a dataclass config. Ported from LinkedIn scraper."""
    for key, value in data.items():
        if hasattr(config, key):
            attr = getattr(config, key)
            if _is_dataclass_instance(attr) and isinstance(value, dict):
                for sub_key, sub_val in value.items():
                    if hasattr(attr, sub_key):
                        setattr(attr, sub_key, sub_val)
            else:
                setattr(config, key, value)


def _is_dataclass_instance(obj) -> bool:
    return hasattr(obj, "__dataclass_fields__")


def _apply_env_overrides(config: Config) -> None:
    email = os.environ.get("SIGNALS_CONTACT_EMAIL")
    if email:
        config.contact_email = email
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        config.github_token = token
    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        config.alert_webhook_url = webhook
    db_path = os.environ.get("SIGNALS_DB_PATH")
    if db_path:
        config.storage.db_path = db_path
    cf_api_key = os.environ.get("CLOUDFLARE_SOLVER_API_KEY")
    if cf_api_key:
        config.cloudflare.solver_api_key = cf_api_key
    cf_provider = os.environ.get("CLOUDFLARE_SOLVER_PROVIDER")
    if cf_provider:
        config.cloudflare.solver_provider = cf_provider
    cf_strategy = os.environ.get("CLOUDFLARE_BYPASS_STRATEGY")
    if cf_strategy:
        config.cloudflare.bypass_strategy = cf_strategy
    cf_headed = os.environ.get("CLOUDFLARE_HEADED_FALLBACK")
    if cf_headed is not None and cf_headed != "":
        config.cloudflare.headed_fallback = cf_headed.strip().lower() in ("1", "true", "yes", "on")

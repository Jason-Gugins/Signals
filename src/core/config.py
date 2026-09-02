"""Config loader — YAML + env vars + CLI overrides.

Dataclass-based config with nested sections matching config/default.yaml.
`_apply_dict_to_config` is copied from ../Linkedin/src/config.py.
"""

from __future__ import annotations

import json
import logging
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
class DataDomeConfig:
    enabled: bool = False
    bypass_strategy: str = "solver"
    solver_provider: str | None = None
    solver_api_key: str | None = None
    headed_fallback: bool = False
    cookie_ttl_hours: int = 24
    residential_proxy: str | None = None


@dataclass
class AntibotConfig:
    enabled: bool = False
    fallback: str = "curl_cffi"
    recheck_after_hours: int = 24


@dataclass
class SmtpConfig:
    """Outbound SMTP for brief/digest email delivery (Task 27).

    user_env / pass_env hold the NAMES of env vars whose values are the
    credentials (resolved at send time) — never the credentials themselves.
    """

    host: Optional[str] = None
    port: int = 587
    user_env: str = "SIGNALS_SMTP_USER"
    pass_env: str = "SIGNALS_SMTP_PASS"
    use_tls: bool = True


@dataclass
class CookiesConfig:
    """Persistent cookie jars for the core fetchers (Task 23).

    Disabled by default — nothing changes until config/default.yaml sets
    cookies.enabled: true. When enabled, http/curl/browser fetchers load a
    per-scope jar from data/cookies/<scope>.json at collect start and save
    it back at collect end.
    """

    enabled: bool = False


@dataclass
class RerankConfig:
    """Optional cross-encoder relevance reranking of SERP candidates.

    Disabled by default — zero behavior change until enabled in
    config/default.yaml. `floor` is the minimum relevance score (0..1)
    a news item must score to survive into classification.
    """

    enabled: bool = False
    floor: float = 0.35


@dataclass
class StorageConfig:
    db_path: str = "data/signals.db"
    raw_dir: str = "data/raw"
    export_dir: str = "data/exports"
    briefs_dir: str = "data/briefs"
    alerts_dir: str = "data/alerts"
    digests_dir: str = "data/digests"
    recon_dir: str = "data/recon"
    keep_raw_days: int = 400
    raw_quota_mb: float | None = None  # null = disabled


@dataclass
class ExternalDbConfig:
    linkedin_db: str = "../Linkedin/data/linkedin.db"
    repvue_db: str = "../repvue-scraper/data/repvue.db"
    linkedin_cli_cwd: str = "../Linkedin"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/logs/signals.log"
    logs_dir: str = "data/logs"
    rotation: str = "10 MB"


@dataclass
class PipelineConfig:
    default_lookback_days: int = 365
    score_on_collect: bool = True
    alert_on_new_primary: bool = True


@dataclass
class ExportsConfig:
    """Export destination plugins (Task 14).

    destinations: list of {type: "file"|"webhook", ...}. Default [{type: file}]
    preserves the pre-plugin behavior. Webhook destinations reuse the existing
    signed webhook delivery path (deliver_alerts/post_json_webhook) — no
    parallel delivery system.
    """

    destinations: list = field(default_factory=lambda: [{"type": "file"}])


@dataclass
class Config:
    http: HttpConfig = field(default_factory=HttpConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    cloudflare: CloudflareConfig = field(default_factory=CloudflareConfig)
    datadome: DataDomeConfig = field(default_factory=DataDomeConfig)
    antibot: AntibotConfig = field(default_factory=AntibotConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    external_dbs: ExternalDbConfig = field(default_factory=ExternalDbConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    contact_email: Optional[str] = None
    github_token: Optional[str] = None
    alert_webhook_url: Optional[str] = None
    alert_webhook_timeout_s: float = 10.0
    # Outbound alert webhooks: list of {url, format: "slack"|"json", secret_env?}.
    # secret_env is the NAME of an env var holding the signing secret (resolved at
    # send time) — never the secret itself. Populated from ALERT_WEBHOOKS_JSON.
    alert_webhooks: list = field(default_factory=list)
    # Per-tier alert routing (Task 12): [{min_tier, max_tier, webhooks, digest?}].
    # Empty = legacy behavior (all alerts to all configured webhooks).
    # Populated from ALERT_ROUTES_JSON.
    alert_routes: list = field(default_factory=list)
    exports: "ExportsConfig" = field(default_factory=lambda: ExportsConfig())
    cookies: "CookiesConfig" = field(default_factory=lambda: CookiesConfig())
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    rerank: "RerankConfig" = field(default_factory=lambda: RerankConfig())
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


def _parse_alert_webhooks_json(raw: str) -> list:
    """Parse ALERT_WEBHOOKS_JSON: a JSON array of {url, format, secret_env?}.
    Anything invalid (bad JSON, not an array, non-dict entries) -> [] + warning."""
    logger = logging.getLogger(__name__)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("ALERT_WEBHOOKS_JSON ignored: invalid JSON (%s)", exc)
        return []
    if not isinstance(parsed, list):
        logger.warning("ALERT_WEBHOOKS_JSON ignored: expected a JSON array of objects")
        return []
    out = []
    for entry in parsed:
        if not isinstance(entry, dict):
            logger.warning("ALERT_WEBHOOKS_JSON entry ignored: expected an object, got %r", entry)
            continue
        out.append(entry)
    return out


def _parse_alert_routes_json(raw: str) -> list:
    """Parse ALERT_ROUTES_JSON: a JSON array of {min_tier, max_tier, webhooks, digest?}.
    Anything invalid (bad JSON, not an array, non-dict entries) -> [] + warning."""
    logger = logging.getLogger(__name__)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("ALERT_ROUTES_JSON ignored: invalid JSON (%s)", exc)
        return []
    if not isinstance(parsed, list):
        logger.warning("ALERT_ROUTES_JSON ignored: expected a JSON array of objects")
        return []
    out = []
    for entry in parsed:
        if not isinstance(entry, dict):
            logger.warning("ALERT_ROUTES_JSON entry ignored: expected an object, got %r", entry)
            continue
        out.append(entry)
    return out


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
    webhooks_raw = os.environ.get("ALERT_WEBHOOKS_JSON")
    if webhooks_raw:
        config.alert_webhooks = _parse_alert_webhooks_json(webhooks_raw)
    routes_raw = os.environ.get("ALERT_ROUTES_JSON")
    if routes_raw:
        config.alert_routes = _parse_alert_routes_json(routes_raw)
    webhook_timeout = os.environ.get("ALERT_WEBHOOK_TIMEOUT_S")
    if webhook_timeout:
        try:
            config.alert_webhook_timeout_s = float(webhook_timeout)
        except ValueError:
            pass
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
    dd_provider = os.environ.get("DATADOME_SOLVER_PROVIDER")
    if dd_provider:
        config.datadome.solver_provider = dd_provider
    dd_api_key = os.environ.get("DATADOME_SOLVER_API_KEY")
    if dd_api_key:
        config.datadome.solver_api_key = dd_api_key
    dd_proxy = os.environ.get("DATADOME_RESIDENTIAL_PROXY")
    if dd_proxy:
        config.datadome.residential_proxy = dd_proxy
    smtp_host = os.environ.get("SIGNALS_SMTP_HOST")
    if smtp_host:
        config.smtp.host = smtp_host
    smtp_port = os.environ.get("SIGNALS_SMTP_PORT")
    if smtp_port:
        try:
            config.smtp.port = int(smtp_port)
        except ValueError:
            pass
    smtp_user_env = os.environ.get("SIGNALS_SMTP_USER_ENV")
    if smtp_user_env:
        config.smtp.user_env = smtp_user_env
    smtp_pass_env = os.environ.get("SIGNALS_SMTP_PASS_ENV")
    if smtp_pass_env:
        config.smtp.pass_env = smtp_pass_env

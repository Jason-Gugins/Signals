"""Tests for SMTP email delivery (Task 27)."""

from __future__ import annotations

import smtplib
from email.message import Message

import pytest
from click.testing import CliRunner
from loguru import logger

from src.cli import main
from src.core.config import Config, SmtpConfig
from src.export.email import send_email


@pytest.fixture
def warn_log():
    """Capture loguru records at WARNING+."""
    records: list = []

    def sink(message):
        records.append(str(message))

    handler_id = logger.add(sink, level="WARNING")
    yield records
    logger.remove(handler_id)


class FakeSMTP:
    """smtplib.SMTP-like fake; captures the sent message."""

    def __init__(self, host, port, timeout=None, *, fail_send=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fail_send = fail_send
        self.calls: list[tuple] = []
        self.message: Message | None = None
        self._ehlo_done = False

    def ehlo(self):
        self._ehlo_done = True
        self.calls.append(("ehlo",))
        return (250, b"ok")

    def starttls(self):
        assert self._ehlo_done, "starttls before EHLO"
        self.calls.append(("starttls",))
        return (220, b"ready")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        return (235, b"ok")

    def send_message(self, msg):
        if self.fail_send:
            raise smtplib.SMTPException("send failed")
        self.message = msg
        self.calls.append(("send_message",))

    def quit(self):
        self.calls.append(("quit",))
        return (221, b"bye")


def _factory(log, **fake_kw):
    def make(host, port, timeout=None):
        s = FakeSMTP(host, port, timeout, **fake_kw)
        log.append(s)
        return s

    return make


def _env(monkeypatch):
    monkeypatch.setenv("MY_USER_ENV", "user@example.com")
    monkeypatch.setenv("MY_PASS_ENV", "secret")


def test_payload_shape(monkeypatch):
    _env(monkeypatch)
    log: list = []
    ok = send_email(
        "Hi there",
        "body text",
        to="dest@example.com",
        host="smtp.example.com",
        port=587,
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory(log),
    )
    assert ok is True
    (smtp,) = log
    msg = smtp.message
    assert msg["Subject"] == "Hi there"
    assert msg["From"] == "user@example.com"
    assert msg["To"] == "dest@example.com"
    assert "body text" in msg.get_payload(decode=True).decode("utf-8")
    # full call sequence: EHLO, STARTTLS, login, send, quit
    kinds = [c[0] for c in smtp.calls]
    assert kinds == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]


def test_tls_default_on(monkeypatch):
    _env(monkeypatch)
    log: list = []
    send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory(log),
    )
    (smtp,) = log
    assert ("starttls",) in smtp.calls
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.timeout == 30


def test_tls_off_skips_starttls(monkeypatch):
    _env(monkeypatch)
    log: list = []
    send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        use_tls=False,
        smtp_factory=_factory(log),
    )
    (smtp,) = log
    assert ("starttls",) not in smtp.calls


def test_auth_credentials_from_env(monkeypatch):
    _env(monkeypatch)
    log: list = []
    send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory(log),
    )
    (smtp,) = log
    login = [c for c in smtp.calls if c[0] == "login"][0]
    assert login[1] == "user@example.com"
    assert login[2] == "secret"


def test_missing_user_env_warns_and_returns_false(monkeypatch, warn_log):
    monkeypatch.delenv("MY_USER_ENV", raising=False)
    monkeypatch.delenv("MY_PASS_ENV", raising=False)
    log: list = []
    ok = send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory(log),
    )
    assert ok is False
    assert log == []  # never connected
    assert warn_log and "MY_USER_ENV" in warn_log[0]


def test_missing_pass_env_returns_false(monkeypatch, warn_log):
    monkeypatch.setenv("MY_USER_ENV", "u@example.com")
    monkeypatch.delenv("MY_PASS_ENV", raising=False)
    ok = send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory([]),
    )
    assert ok is False
    assert warn_log and "MY_PASS_ENV" in warn_log[0]


def test_smtp_exception_returns_false_not_raised(monkeypatch, warn_log):
    _env(monkeypatch)
    ok = send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=_factory([], fail_send=True),
    )
    assert ok is False
    assert warn_log


def test_oserror_returns_false(monkeypatch):
    _env(monkeypatch)

    def boom_factory(host, port, timeout=None):
        raise OSError("connection refused")

    ok = send_email(
        "s",
        "b",
        to="d@example.com",
        host="smtp.example.com",
        user_env="MY_USER_ENV",
        pass_env="MY_PASS_ENV",
        smtp_factory=boom_factory,
    )
    assert ok is False


def test_smtp_config_defaults():
    cfg = Config()
    assert isinstance(cfg.smtp, SmtpConfig)
    assert cfg.smtp.host is None
    assert cfg.smtp.port == 587
    assert cfg.smtp.user_env == "SIGNALS_SMTP_USER"
    assert cfg.smtp.pass_env == "SIGNALS_SMTP_PASS"
    assert cfg.smtp.use_tls is True


def test_smtp_config_env_application(monkeypatch):
    monkeypatch.setenv("SIGNALS_SMTP_HOST", "smtp.test.org")
    monkeypatch.setenv("SIGNALS_SMTP_PORT", "465")
    monkeypatch.setenv("SIGNALS_SMTP_USER_ENV", "ALT_USER")
    monkeypatch.setenv("SIGNALS_SMTP_PASS_ENV", "ALT_PASS")
    cfg = Config.load(yaml_path="/nonexistent/default.yaml")
    assert cfg.smtp.host == "smtp.test.org"
    assert cfg.smtp.port == 465
    assert cfg.smtp.user_env == "ALT_USER"
    assert cfg.smtp.pass_env == "ALT_PASS"


def test_digest_email_sends_digest_text(tmp_path, monkeypatch):
    """digest --email reads the written file and sends it via send_email."""
    monkeypatch.setenv("SIGNALS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SIGNALS_SMTP_USER", "u@example.com")
    monkeypatch.setenv("SIGNALS_SMTP_PASS", "pw")
    monkeypatch.setenv("SIGNALS_EMAIL_TO", "dest@example.com")
    digest_path = tmp_path / "acme.com.md"
    digest_path.write_text("# Digest for acme.com\nbody", encoding="utf-8")
    monkeypatch.setattr(
        "src.cli._digest_paths", lambda ctx, period, domains: [str(digest_path)]
    )

    log: list = []
    monkeypatch.setattr(
        "src.export.email.smtplib.SMTP", _factory(log)
    )

    runner = CliRunner()
    res = runner.invoke(main, ["digest", "--period", "weekly", "--email"])
    assert res.exit_code == 0, res.output
    assert len(log) == 1
    msg = log[0].message
    assert msg["Subject"] == "Signals weekly digest — acme.com"
    assert msg["To"] == "dest@example.com"
    assert "# Digest for acme.com" in msg.get_payload(decode=True).decode("utf-8")
    logins = [c for c in log[0].calls if c[0] == "login"]
    assert logins == [("login", "u@example.com", "pw")]


def test_digest_email_to_option_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SIGNALS_SMTP_USER", "u@example.com")
    monkeypatch.setenv("SIGNALS_SMTP_PASS", "pw")
    monkeypatch.setenv("SIGNALS_EMAIL_TO", "from-env@example.com")
    digest_path = tmp_path / "acme.com.md"
    digest_path.write_text("# d", encoding="utf-8")
    monkeypatch.setattr(
        "src.cli._digest_paths", lambda ctx, period, domains: [str(digest_path)]
    )
    log: list = []
    monkeypatch.setattr("src.export.email.smtplib.SMTP", _factory(log))
    runner = CliRunner()
    res = runner.invoke(main, ["digest", "--email", "--to", "cli@example.com"])
    assert res.exit_code == 0, res.output
    assert log[0].message["To"] == "cli@example.com"


def test_digest_email_not_configured_warns_and_skips(tmp_path, monkeypatch, warn_log):
    monkeypatch.delenv("SIGNALS_SMTP_HOST", raising=False)
    digest_path = tmp_path / "acme.com.md"
    digest_path.write_text("# d", encoding="utf-8")
    monkeypatch.setattr(
        "src.cli._digest_paths", lambda ctx, period, domains: [str(digest_path)]
    )
    log: list = []
    monkeypatch.setattr("src.export.email.smtplib.SMTP", _factory(log))
    runner = CliRunner()
    res = runner.invoke(main, ["digest", "--email"])
    assert res.exit_code == 0
    assert log == []  # nothing sent
    assert warn_log and "SMTP" in warn_log[0]


def test_digest_send_failure_still_writes_file(tmp_path, monkeypatch, warn_log):
    """A send failure is logged; the digest file still exists and CLI continues."""
    monkeypatch.setenv("SIGNALS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SIGNALS_SMTP_USER", "u@example.com")
    monkeypatch.setenv("SIGNALS_SMTP_PASS", "pw")
    monkeypatch.setenv("SIGNALS_EMAIL_TO", "dest@example.com")
    digest_path = tmp_path / "acme.com.md"
    digest_path.write_text("# d", encoding="utf-8")
    monkeypatch.setattr(
        "src.cli._digest_paths", lambda ctx, period, domains: [str(digest_path)]
    )

    def make(host, port, timeout=None):
        return FakeSMTP(host, port, timeout, fail_send=True)

    monkeypatch.setattr("src.export.email.smtplib.SMTP", make)
    runner = CliRunner()
    res = runner.invoke(main, ["digest", "--email"])
    assert res.exit_code == 0
    assert digest_path.exists()
    assert warn_log


def test_brief_email_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SIGNALS_SMTP_USER", "u@example.com")
    monkeypatch.setenv("SIGNALS_SMTP_PASS", "pw")
    brief_path = tmp_path / "acme.com.md"
    brief_path.write_text("# Brief", encoding="utf-8")
    monkeypatch.setattr(
        "src.cli._brief_paths", lambda ctx, domains, tier_max: [str(brief_path)]
    )
    log: list = []
    monkeypatch.setattr("src.export.email.smtplib.SMTP", _factory(log))

    runner = CliRunner()
    res = runner.invoke(main, ["brief", "--email", "--to", "me@example.com"])
    assert res.exit_code == 0, res.output
    msg = log[0].message
    assert msg["Subject"] == "Signals brief — acme.com"
    assert msg["To"] == "me@example.com"
    assert "# Brief" in msg.get_payload(decode=True).decode("utf-8")


def test_env_example_documents_smtp():
    text = open(".env.example", encoding="utf-8").read()
    for key in (
        "SIGNALS_SMTP_HOST",
        "SIGNALS_SMTP_PORT",
        "SIGNALS_SMTP_USER_ENV",
        "SIGNALS_SMTP_PASS_ENV",
        "SIGNALS_EMAIL_TO",
    ):
        assert key in text
    # shape-only dummy values, not real credentials
    assert "smtp.example.com" in text

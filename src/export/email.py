"""SMTP email delivery (Task 27).

Stdlib-only (smtplib + email.mime) — zero new deps. Credentials are NEVER
stored in config: user_env/pass_env name environment variables whose values
are the username/app-password, resolved at send time. Config problems
(missing env vars) and SMTP/network failures are logged warnings that return
False — send_email never raises for them.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from typing import Callable, Optional

from loguru import logger


def send_email(
    subject: str,
    body: str,
    *,
    to: str,
    host: str,
    port: int = 587,
    user_env: str,
    pass_env: str,
    use_tls: bool = True,
    timeout_s: int = 30,
    smtp_factory: Optional[Callable[..., smtplib.SMTP]] = None,
) -> bool:
    """Send a plain-text (markdown) email via SMTP. Returns True on success.

    user_env / pass_env name env vars holding the actual credentials; they are
    read at send time. Missing env vars -> warning + False (never raises).
    """
    user = os.environ.get(user_env)
    password = os.environ.get(pass_env)
    if not user:
        logger.warning("email skipped: env var {} not set (SMTP user)", user_env)
        return False
    if not password:
        logger.warning("email skipped: env var {} not set (SMTP password)", pass_env)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to

    factory = smtp_factory or smtplib.SMTP
    try:
        client = factory(host, port, timeout=timeout_s)
        client.ehlo()
        if use_tls:
            client.starttls()
            client.ehlo()
        client.login(user, password)
        client.send_message(msg)
        client.quit()
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("email send to {} via {}:{} failed: {}", to, host, port, exc)
        return False
    return True

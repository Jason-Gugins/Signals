"""Registrable-domain normalization. Pure — no I/O."""

from __future__ import annotations

import re
from urllib.parse import urlparse


PUBLIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.ca",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "aol.com",
        "gmx.com",
        "mail.com",
        "zoho.com",
        "fastmail.com",
    }
)

HOSTING_DOMAINS: frozenset[str] = frozenset(
    {
        "wixsite.com",
        "squarespace.com",
        "github.io",
        "herokuapp.com",
        "netlify.app",
        "vercel.app",
        "webflow.io",
        "wordpress.com",
        "blogspot.com",
        "myshopify.com",
    }
)

MULTI_LABEL_TLDS: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "com.br",
        "co.jp",
        "co.in",
        "com.mx",
        "co.za",
        "com.sg",
        "co.kr",
        "com.hk",
        "com.ar",
        "com.tr",
    }
)

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV6_HINT = re.compile(r":")


def is_public_email_domain(d: str) -> bool:
    return d.casefold().removeprefix("www.") in PUBLIC_EMAIL_DOMAINS


def _registrable(host: str) -> str:
    host = host.casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for tld in sorted(MULTI_LABEL_TLDS, key=len, reverse=True):
        suffix = "." + tld
        if host == tld:
            return host
        if host.endswith(suffix):
            head = host[: -len(suffix)]
            label = head.rsplit(".", 1)[-1]
            return f"{label}{suffix}"
    if "." in host:
        parts = host.split(".")
        return ".".join(parts[-2:])
    return host


def root_domain(value: str | None) -> str | None:
    """URL/email/host -> registrable domain, lowercase, no www, no port, no path."""
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    host = ""
    if "@" in s and "://" not in s.split("@", 1)[0]:
        host = s.rsplit("@", 1)[-1]
    else:
        candidate = s if "://" in s else f"https://{s}"
        parsed = urlparse(candidate)
        host = parsed.netloc or parsed.path.split("/")[0]
    host = host.strip().rstrip(".")
    if ":" in host and not host.startswith("["):
        # strip port; keep IPv6 aside
        if _IPV4.match(host.split(":")[0]) or not _IPV6_HINT.search(host.split(":")[0]):
            host = host.rsplit(":", 1)[0]
    host = host.strip("[]").rstrip(".").casefold()
    if not host:
        return None
    if host in {"localhost", "localhost.localdomain"}:
        return None
    if _IPV4.match(host):
        return None
    if ":" in host:  # IPv6
        return None
    root = _registrable(host)
    if not root or "." not in root:
        return None
    if is_public_email_domain(root):
        return None
    return root


def same_org(a: str | None, b: str | None) -> bool:
    ra = root_domain(a) or (a.casefold().removeprefix("www.").rstrip(".") if a else None)
    rb = root_domain(b) or (b.casefold().removeprefix("www.").rstrip(".") if b else None)
    if not ra or not rb:
        return False
    if ra == rb:
        return True
    return ra.endswith("." + rb) or rb.endswith("." + ra)


def domain_variants(d: str) -> list[str]:
    root = root_domain(d) or d.casefold().removeprefix("www.").rstrip(".")
    return [root, f"www.{root}", f"get.{root}"]

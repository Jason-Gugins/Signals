"""Persistent browser-like cookie jar (temporal stealth — Task 3 remainder).

A browser-profile-like jar that accumulates Set-Cookies from every response
and re-sends matching cookies on subsequent fetches, persisted to
``data/antibot/cookies.json`` across process runs. Real browsers always keep
cookie memory; scrapers don't — this closes that temporal gap.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class CookieEntry:
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None  # epoch seconds; None = session-length


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _domain_suffix_match(host: str, domain: str) -> bool:
    """RFC 6265 domain-match: cookie for ``.example.com`` matches
    ``www.example.com``; exact host match always matches."""
    host = host.lower()
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _parse_set_cookie(raw: str, default_domain: str, clock) -> CookieEntry | None:
    """Parse one Set-Cookie header value into a CookieEntry."""
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    for name, morsel in cookie.items():
        attrs = morsel
        expires: float | None = None
        max_age = attrs["max-age"]
        if max_age:
            try:
                max_age_val = int(max_age)
                if max_age_val <= 0:
                    return None  # expired immediately — do not store
                expires = clock() + max_age_val
            except ValueError:
                pass
        elif attrs["expires"]:
            try:
                expires = time.mktime(time.strptime(attrs["expires"], "%a, %d %b %Y %H:%M:%S %Z"))
            except (ValueError, OverflowError):
                try:
                    from email.utils import parsedate_to_datetime

                    expires = parsedate_to_datetime(attrs["expires"]).timestamp()
                except Exception:
                    expires = None
        domain = attrs["domain"] or default_domain
        return CookieEntry(
            name=name,
            value=morsel.value or "",
            domain=domain,
            path=attrs["path"] or "/",
            expires=expires,
        )
    return None


def _split_folded_set_cookie(value: str) -> list[str]:
    """Split a comma-folded set-cookie header into individual cookies,
    keeping comma-dated ``expires=Wed, 21 Oct ...`` intact (a continuation
    segment starts with a digit and follows an ``expires=`` fragment)."""
    parts = value.split(", ")
    out: list[str] = [parts[0]] if parts else []
    for seg in parts[1:]:
        prev = out[-1]
        last_attr = prev.split(";")[-1].strip().lower()
        if seg[:1].isdigit() and last_attr.startswith("expires="):
            out[-1] = f"{out[-1]}, {seg}"
        else:
            out.append(seg)
    return out


class PersistentCookieJar:
    """Cookie store keyed by (domain, name), persisted as JSON."""

    def __init__(self, path: str | Path = "data/antibot/cookies.json", clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._cookies: dict[tuple[str, str], CookieEntry] = {}

    # -- ingest ---------------------------------------------------------------

    def set_from_response(self, url: str, headers) -> None:
        """Accumulate cookies from a response.

        ``headers`` may be a dict (set-cookie comma-folded, as the transport
        builds it) or a list of (name, value) pairs (as the engine JSON
        carries them). Multiple set-cookie values are handled either way.
        """
        host = _host_of(url)
        raws: list[str] = []
        if isinstance(headers, dict):
            if headers.get("set-cookie"):
                raws = _split_folded_set_cookie(headers["set-cookie"])
        else:
            for name, value in headers or []:
                if name.lower() == "set-cookie":
                    raws.append(value)

        for raw in raws:
            if "=" not in raw:
                continue
            entry = _parse_set_cookie(raw, host, self.clock)
            if entry is None:
                continue
            self._cookies[(entry.domain, entry.name)] = entry

    # -- emit -----------------------------------------------------------------

    def cookies_for(self, url: str) -> list[dict]:
        """Cookies to send for ``url``: host/domain-suffix matched, not
        expired, shaped ``[{"name", "value", "domain"}]``."""
        host = _host_of(url)
        now = self.clock()
        out = []
        for entry in self._cookies.values():
            if entry.expires is not None and entry.expires <= now:
                continue
            if _domain_suffix_match(host, entry.domain):
                out.append(
                    {"name": entry.name, "value": entry.value, "domain": entry.domain}
                )
        return out

    # -- management -----------------------------------------------------------

    def update(self, name: str, value: str, domain: str, expires: float | None = None) -> None:
        self._cookies[(domain, name)] = CookieEntry(
            name=name, value=value, domain=domain, expires=expires
        )

    def clear(self, domain: str | None = None) -> None:
        if domain is None:
            self._cookies.clear()
            return
        for key in [k for k in self._cookies if k[0] == domain]:
            del self._cookies[key]

    # -- persistence ------------------------------------------------------------

    def load(self) -> None:
        """Load from disk; tolerate a missing or corrupt file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        for item in data:
            try:
                entry = CookieEntry(
                    name=item["name"],
                    value=item["value"],
                    domain=item["domain"],
                    path=item.get("path", "/"),
                    expires=item.get("expires"),
                )
            except (KeyError, TypeError):
                continue
            self._cookies[(entry.domain, entry.name)] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "name": e.name,
                "value": e.value,
                "domain": e.domain,
                "path": e.path,
                "expires": e.expires,
            }
            for e in self._cookies.values()
        ]
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


__all__ = ["CookieEntry", "PersistentCookieJar"]

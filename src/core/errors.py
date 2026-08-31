"""Fetch error taxonomy (P2 Task 2).

Classifies fetch failures into a small closed enum and gives each class a
backoff multiplier the runner folds into its next-due computation. Pure
module: no I/O, no imports beyond the stdlib.
"""

from __future__ import annotations

from enum import Enum

# Body markers that identify a Cloudflare challenge interstitial.
_CF_MARKERS = (
    "cf-challenge",
    "just a moment",
    "challenges.cloudflare",
)

# Error-string markers for DNS failures.
_DNS_MARKERS = (
    "dns",
    "getaddrinfo",
    "name or service",
)


class FetchErrorClass(str, Enum):
    """Closed taxonomy of fetch failure classes."""

    challenge = "challenge"
    timeout = "timeout"
    dns = "dns"
    ratelimit = "ratelimit"
    parse_drift = "parse_drift"
    auth = "auth"
    other = "other"


# Per-class penalty multiplier applied to the runner's next-due backoff.
BACKOFF_MULTIPLIER: dict[FetchErrorClass, int] = {
    FetchErrorClass.challenge: 4,
    FetchErrorClass.ratelimit: 8,
    FetchErrorClass.timeout: 2,
    FetchErrorClass.dns: 3,
    FetchErrorClass.auth: 8,
    FetchErrorClass.parse_drift: 1,
    FetchErrorClass.other: 2,
}


def _contains(text: str | None, markers: tuple[str, ...]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def classify_fetch_error(
    status: int | None, error: str | None, body_hint: str | None
) -> FetchErrorClass:
    """Classify a fetch failure.

    Precedence: challenge (403 + CF body marker) > ratelimit (429) >
    timeout > dns > auth (401/403 without challenge markers) > other.
    """
    if status == 403 and _contains(body_hint, _CF_MARKERS):
        return FetchErrorClass.challenge
    if status == 429:
        return FetchErrorClass.ratelimit
    if _contains(error, ("timed out", "timeout")):
        return FetchErrorClass.timeout
    if _contains(error, _DNS_MARKERS):
        return FetchErrorClass.dns
    if status in (401, 403):
        return FetchErrorClass.auth
    return FetchErrorClass.other

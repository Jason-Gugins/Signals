"""Source-agnostic five-state self-check runner.

Fetches one known-good sample URL via an injected fetcher and verifies a
parser still extracts items. States:
  ok        — >=1 item parsed
  drift     — source-specific markup present but 0 parsed (selectors stale)
  empty     — page genuinely has no markup (nothing to parse)
  challenge — anti-bot interstitial (network problem, not parser problem)
  error     — fetch failed or returned no document
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class SelfcheckResult:
    state: str
    review_count: int
    url: str
    detail: str = ""


def run_source_selfcheck(
    fetcher,
    *,
    url: str,
    source: str,
    domain: str,
    extract: Callable[[str], list],
    markup_hints: Sequence[bytes] = (),
    detect_challenge: Callable[..., bool] | None = None,
    fetch_kwargs: dict | None = None,
    drift_detail: str = "markup present but parser extracted 0 — selectors stale",
) -> SelfcheckResult:
    """Run the five-state selfcheck for any source.

    ``fetcher.fetch(url, source=..., domain=..., **fetch_kwargs)`` must return
    a FetchResult-like object with ``.ok``, ``.status`` and ``.doc`` (``.doc``
    may be None on failure). ``extract`` receives the decoded body text and
    returns a list of parsed items; ``markup_hints`` are byte needles that
    identify the source's markup for drift detection; ``detect_challenge``
    (optional) receives ``(status=..., body=...)`` and returns True when the
    response is an anti-bot interstitial.
    """
    try:
        result = fetcher.fetch(url, source=source, domain=domain,
                               **(fetch_kwargs or {}))
    except Exception as e:  # noqa: BLE001 — any fetch failure is the 'error' state
        return SelfcheckResult("error", 0, url, detail=str(e))
    if result is None or not result.ok or result.doc is None:
        return SelfcheckResult("error", 0, url, detail="fetch returned no document")
    body = result.doc.body or b""
    if detect_challenge is not None and detect_challenge(status=result.status, body=body):
        return SelfcheckResult("challenge", 0, url)
    html = body.decode("utf-8", "replace")
    items = extract(html) or []
    if items:
        return SelfcheckResult("ok", len(items), url)
    if any(h in body for h in markup_hints):
        return SelfcheckResult("drift", 0, url, detail=drift_detail)
    return SelfcheckResult("empty", 0, url)

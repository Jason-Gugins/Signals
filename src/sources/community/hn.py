"""Hacker News Algolia parser. PURE."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import quote_plus

from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.names import normalize_name
from src.sources.base import SignalCandidate


def hn_url(name: str, since_epoch: int, tags: str = "story") -> str:
    return f"https://hn.algolia.com/api/v1/search_by_date?query=%22{quote_plus(name)}%22&tags={tags}&numericFilters=created_at_i>{since_epoch}"


def parse_hn(body: bytes) -> list[dict]:
    raw = json.loads(body)
    out = []
    for h in raw.get("hits") or []:
        out.append(
            {
                "objectID": h.get("objectID"),
                "title": h.get("title"),
                "url": h.get("url"),
                "created_at": h.get("created_at"),
                "points": h.get("points") or 0,
                "num_comments": h.get("num_comments") or 0,
                "author": h.get("author"),
            }
        )
    return out


def hn_to_candidates(hits, account: Account, *, today: date) -> list[SignalCandidate]:
    want = normalize_name(account.name) if account.name else ""
    out = []
    for h in hits:
        title = h.get("title") or ""
        if want and want not in (normalize_name(title) or ""):
            continue
        points = int(h.get("points") or 0)
        conf = min(0.7, 0.4 + 0.1 * (points // 50))
        out.append(
            SignalCandidate(
                signal_type="intent_3rd_topic",
                observed_at=(h.get("created_at") or today.isoformat())[:10],
                natural_key=f"hn:{h.get('objectID')}",
                title=title,
                url=h.get("url"),
                confidence=conf,
                evidence_data={"points": points},
            )
        )
        import re
        if re.search(r"\b(launch\w*|introducing|announcing)\b", title, re.I):
            host = root_domain(h.get("url"))
            if host and host == account.domain:
                out.append(
                    SignalCandidate(
                        signal_type="product_launch",
                        observed_at=(h.get("created_at") or today.isoformat())[:10],
                        natural_key=f"hnlaunch:{h.get('objectID')}",
                        title=title,
                        url=h.get("url"),
                        confidence=0.6,
                        evidence_data={},
                    )
                )
    return out

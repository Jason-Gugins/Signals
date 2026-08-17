"""iTunes Search API parser. PURE."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import quote_plus

from src.core.models import Account, Contact
from src.identity.names import normalize_name
from src.sources.base import SignalCandidate


def itunes_url(term: str, *, entity="podcastEpisode", limit=50) -> str:
    return f"https://itunes.apple.com/search?media=podcast&entity={entity}&term={quote_plus(term)}&limit={limit}"


def parse_itunes(body: bytes) -> list[dict]:
    raw = json.loads(body)
    out = []
    for it in raw.get("results") or []:
        out.append(
            {
                "trackName": it.get("trackName"),
                "collectionName": it.get("collectionName"),
                "releaseDate": it.get("releaseDate"),
                "trackViewUrl": it.get("trackViewUrl"),
                "description": it.get("description") or it.get("shortDescription"),
            }
        )
    return out


def itunes_to_candidates(items, account: Account, contacts, *, today: date) -> list[SignalCandidate]:
    company = normalize_name(account.name) if account.name else ""
    out = []
    for it in items:
        blob = f"{it.get('trackName') or ''} {it.get('description') or ''}"
        blob_n = normalize_name(blob) or ""
        person = None
        for c in contacts:
            if c.name and normalize_name(c.name) and normalize_name(c.name) in blob_n:
                person = c
                break
        if not person and company not in blob_n:
            continue
        out.append(
            SignalCandidate(
                signal_type="content_appearance",
                observed_at=(it.get("releaseDate") or today.isoformat())[:10],
                natural_key=f"pod:{it.get('trackViewUrl')}",
                title=it.get("trackName"),
                url=it.get("trackViewUrl"),
                confidence=0.65,
                person_key=person.person_key if person else None,
                evidence_data={"show": it.get("collectionName")},
            )
        )
    return out

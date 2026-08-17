"""Federal Register document parser. PURE."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlencode

from src.core.models import Account
from src.sources.base import SignalCandidate


def fr_query_url(terms: list[str], since: str, types=("RULE", "PRORULE")) -> str:
    q = [("per_page", "100"), ("order", "newest"), ("conditions[publication_date][gte]", since)]
    for t in types:
        q.append(("conditions[type][]", t))
    q.append(("conditions[term]", " ".join(terms)))
    return "https://www.federalregister.gov/api/v1/documents.json?" + urlencode(q)


def parse_fr_documents(body: bytes) -> list[dict]:
    raw = json.loads(body)
    out = []
    for d in raw.get("results") or []:
        agencies = d.get("agencies") or []
        names = [a.get("name") for a in agencies if isinstance(a, dict) and a.get("name")]
        out.append(
            {
                "document_number": d.get("document_number"),
                "title": d.get("title"),
                "abstract": d.get("abstract"),
                "publication_date": d.get("publication_date"),
                "effective_on": d.get("effective_on"),
                "html_url": d.get("html_url"),
                "agencies": names,
                "type": d.get("type"),
            }
        )
    return out


def match_watches(doc: dict, watches: list[dict]) -> list[str]:
    blob = f"{doc.get('title') or ''} {doc.get('abstract') or ''}".casefold()
    ids = []
    for w in watches:
        if any(t.casefold() in blob for t in w.get("terms") or []):
            ids.append(w["id"])
    return ids


def fr_to_candidates(doc: dict, watch_ids: list[str], account: Account, *, today: date, watches: list[dict] | None = None) -> list[SignalCandidate]:
    watches = watches or []
    by_id = {w["id"]: w for w in watches}
    out = []
    for wid in watch_ids:
        w = by_id.get(wid) or {}
        industries = [x.casefold() for x in w.get("applies_to_industries") or []]
        prefixes = w.get("applies_to_sic_prefix") or []
        industry_ok = account.industry and account.industry.casefold() in industries
        sic_ok = bool(account.sic_code and any(str(account.sic_code).startswith(p) for p in prefixes))
        if not industry_ok and not sic_ok:
            continue
        out.append(
            SignalCandidate(
                signal_type="regulation_applicable",
                observed_at=doc.get("publication_date") or today.isoformat(),
                natural_key=f"fr:{doc.get('document_number')}:{wid}",
                title=doc.get("title"),
                url=doc.get("html_url"),
                confidence=0.7,
                evidence_data={
                    "regulation": doc.get("title"),
                    "agency": (doc.get("agencies") or [None])[0],
                    "effective_on": doc.get("effective_on"),
                    "watch_id": wid,
                    "doc_url": doc.get("html_url"),
                },
            )
        )
    return out

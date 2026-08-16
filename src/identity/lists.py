"""Load local exclusion lists and champion CSVs."""

from __future__ import annotations

import csv
from pathlib import Path

from src.core.db import Database
from src.core.models import Contact
from src.core.textutil import stable_id
from src.identity.domains import root_domain


def load_domain_list(path: str) -> set[str]:
    out: set[str] = set()
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        domain = root_domain(s)
        if domain:
            out.add(domain)
    return out


def load_champions(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            rec = {(k or "").strip().casefold(): (v or "").strip() for k, v in raw.items()}
            name = rec.get("name") or ""
            slug = rec.get("linkedin_slug") or ""
            prior = rec.get("prior_domain") or ""
            if prior:
                prior = root_domain(prior) or prior.casefold()
            if not name or not (slug or prior):
                continue
            rows.append(
                {
                    "name": name,
                    "linkedin_slug": slug or None,
                    "prior_company": rec.get("prior_company") or None,
                    "prior_domain": prior or None,
                    "relationship": rec.get("relationship") or None,
                    "last_touch": rec.get("last_touch") or None,
                    "notes": rec.get("notes") or None,
                }
            )
    return rows


def upsert_champions(db: Database, rows: list[dict]) -> int:
    n = 0
    for rec in rows:
        slug = rec.get("linkedin_slug")
        key = slug or stable_id(rec.get("name") or "", rec.get("prior_domain") or "")
        contact = Contact(
            person_key=key,
            name=rec.get("name"),
            linkedin_slug=slug,
            linkedin_url=f"https://www.linkedin.com/in/{slug}" if slug else None,
            prior_domain=rec.get("prior_domain"),
            is_champion=True,
            extra_data={
                k: rec[k]
                for k in ("prior_company", "relationship", "last_touch", "notes")
                if rec.get(k)
            },
        )
        db.upsert("contacts", contact.to_db_row(), pk="person_key", overwrite={"is_champion"})
        n += 1
    return n

"""Seed accounts from CSV, LinkedIn DB, and RepVue DB."""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.core.models import Account
from src.core.textutil import parse_count
from src.identity.domains import root_domain
from src.identity.icp import evaluate_icp
from src.identity.registry import AccountRegistry

logger = logging.getLogger(__name__)


@dataclass
class SeedStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _open_ro(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _split_hq(raw: str | None) -> tuple[str | None, str | None, str | None]:
    if not raw:
        return None, None, None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], ", ".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], None, None


def _slug_from_linkedin_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/company/([^/?#]+)", url)
    return m.group(1).strip("/") if m else None


def _record(
    registry: AccountRegistry,
    account: Account,
    stats: SeedStats,
    source: str,
    *,
    icp_rules: dict | None = None,
) -> None:
    existing = registry.get(account.domain)
    if existing and existing.extra_data:
        merged = dict(existing.extra_data)
        merged.update(account.extra_data or {})
        account.extra_data = merged
    registry.upsert(account, source=source)
    # Seed-time ICP scoring: apply config/icp.yaml rules once at insert so
    # tier ordering is real from day one. icp_rules is passed in by the
    # orchestrator ({} when config/icp.yaml is missing) — this module stays
    # I/O-clean. A failure (e.g. unknown predicate) leaves the 1.0 default.
    if icp_rules:
        try:
            result = evaluate_icp(account, icp_rules)
            account.icp_fit = result.multiplier
            account.icp_reasons = result.reasons
            account.disqualified = result.disqualified
            account.disqualify_reason = result.disqualify_reason
            registry.upsert(account, source=source)
        except Exception as exc:
            logger.warning("seed icp evaluation failed for %s: %s", account.domain, exc)
    if existing:
        stats.updated += 1
    else:
        stats.created += 1


def seed_from_csv(
    registry: AccountRegistry,
    path: str | Path,
    *,
    cohort: str | None = None,
    icp_rules: dict | None = None,
) -> SeedStats:
    stats = SeedStats()
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = {(k or "").strip().casefold(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            domain = root_domain(row.get("domain") or row.get("website") or row.get("url"))
            li_url = row.get("linkedin_url") or None
            if not domain:
                stats.skip("pending_domain" if li_url else "no_domain")
                continue
            name = row.get("name") or row.get("company") or None
            emp = parse_count(row.get("employee_count")) if row.get("employee_count") else None
            account = Account(
                domain=domain,
                name=name,
                linkedin_slug=row.get("linkedin_slug") or _slug_from_linkedin_url(li_url),
                ticker=row.get("ticker") or None,
                cik=row.get("cik") or None,
                industry=row.get("industry") or None,
                employee_count=emp,
                hq_country=row.get("hq_country") or None,
                careers_url=row.get("careers_url") or None,
                cohort=cohort,
            )
            _record(registry, account, stats, source="csv", icp_rules=icp_rules)
    return stats


def seed_from_linkedin_db(
    registry: AccountRegistry,
    db_path: str,
    *,
    cohort: str | None = None,
    limit: int | None = None,
    icp_rules: dict | None = None,
) -> SeedStats:
    stats = SeedStats()
    conn = _open_ro(db_path)
    try:
        sql = "SELECT * FROM companies"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    for row in rows:
        rec = dict(row)
        domain = root_domain(rec.get("domain") or rec.get("website"))
        if not domain:
            stats.skip("no_domain")
            continue
        city, region, country = _split_hq(rec.get("headquarters"))
        extra = {}
        if rec.get("headquarters"):
            extra["headquarters_raw"] = rec["headquarters"]
        account = Account(
            domain=domain,
            name=rec.get("name"),
            linkedin_slug=rec.get("linkedin_slug") or _slug_from_linkedin_url(rec.get("linkedin_url")),
            linkedin_company_id=rec.get("linkedin_company_id"),
            industry=rec.get("industry"),
            employee_count=rec.get("employee_count"),
            hq_city=city,
            hq_region=region,
            hq_country=country,
            founded=rec.get("founded"),
            company_type=rec.get("company_type"),
            cohort=cohort or rec.get("cohort"),
            extra_data=extra,
        )
        _record(registry, account, stats, source="linkedin_db", icp_rules=icp_rules)
    return stats


def seed_from_repvue_db(
    registry: AccountRegistry,
    db_path: str,
    *,
    cohort: str | None = None,
    limit: int | None = None,
    icp_rules: dict | None = None,
) -> SeedStats:
    stats = SeedStats()
    conn = _open_ro(db_path)
    try:
        sql = "SELECT * FROM companies"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    for row in rows:
        rec = dict(row)
        domain = root_domain(rec.get("domain") or rec.get("website") or rec.get("url"))
        if not domain:
            stats.skip("no_domain")
            continue
        city, region, country = _split_hq(rec.get("headquarters"))
        extra = {
            "repvue": {
                "repvue_score": rec.get("repvue_score"),
                "ratings_count": rec.get("ratings_count"),
                "quota_attainment": rec.get("quota_attainment"),
            }
        }
        account = Account(
            domain=domain,
            name=rec.get("name"),
            repvue_slug=rec.get("slug"),
            industry=rec.get("industry"),
            hq_city=city,
            hq_region=region,
            hq_country=country,
            cohort=cohort,
            extra_data=extra,
        )
        _record(registry, account, stats, source="repvue_db", icp_rules=icp_rules)
    return stats

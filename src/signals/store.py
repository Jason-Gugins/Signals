"""Persist signals with first-seen / last-seen merge semantics."""

from __future__ import annotations

from typing import Optional

from src.core.db import Database
from src.core.models import Signal
from src.signals.taxonomy import Taxonomy


class SignalStore:
    def __init__(self, db: Database, taxonomy: Taxonomy | None = None):
        self.db = db
        self.taxonomy = taxonomy or Taxonomy.load()

    def get(self, signal_id: str) -> Optional[Signal]:
        row = self.db.one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        return Signal.from_db_row(row) if row else None

    def upsert(self, signal: Signal) -> bool:
        existing = self.get(signal.signal_id)
        if existing is None:
            row = signal.to_db_row()
            if not row.get("first_seen_at"):
                row["first_seen_at"] = signal.observed_at
            if not row.get("last_seen_at"):
                row["last_seen_at"] = signal.observed_at
            self.db.upsert("signals", row, pk="signal_id")
            return True
        merged = self._merge(existing, signal)
        self.db.upsert("signals", merged.to_db_row(), pk="signal_id", overwrite=set(merged.to_db_row()) - {"signal_id"})
        return False

    def _merge(self, old: Signal, new: Signal) -> Signal:
        def _min(a, b):
            if not a:
                return b
            if not b:
                return a
            return a if a <= b else b

        def _max(a, b):
            if not a:
                return b
            if not b:
                return a
            return a if a >= b else b

        return Signal(
            signal_id=old.signal_id,
            domain=old.domain,
            signal_type=old.signal_type,
            category=old.category,
            origin=old.origin,
            catalyst=old.catalyst,
            polarity=old.polarity,
            observed_at=_min(old.observed_at, new.observed_at),
            source=old.source or new.source,
            degree=old.degree,
            person_key=old.person_key or new.person_key,
            title=old.title or new.title,
            summary=old.summary or new.summary,
            evidence=old.evidence or new.evidence,
            evidence_data=old.evidence_data or new.evidence_data,
            url=old.url or new.url,
            confidence=max(old.confidence or 0, new.confidence or 0),
            first_seen_at=_min(old.first_seen_at, new.first_seen_at),
            last_seen_at=_max(old.last_seen_at, new.last_seen_at),
            raw_ref=old.raw_ref or new.raw_ref,
            superseded_by=old.superseded_by or new.superseded_by,
        )

    def upsert_many(self, signals: list[Signal]) -> tuple[int, int]:
        new = updated = 0
        for sig in signals:
            if self.upsert(sig):
                new += 1
            else:
                updated += 1
        return new, updated

    def for_account(
        self,
        domain: str,
        *,
        since: str | None = None,
        types: list[str] | None = None,
        categories: list[str] | None = None,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list[Signal]:
        clauses = ["domain = ?", "confidence >= ?"]
        params: list = [domain, min_confidence]
        if since:
            clauses.append("observed_at >= ?")
            params.append(since)
        if types:
            clauses.append(f"signal_type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if categories:
            clauses.append(f"category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        rows = self.db.query(
            "SELECT * FROM signals WHERE " + " AND ".join(clauses),
            params,
        )
        signals = [Signal.from_db_row(r) for r in rows]

        def weight(sig: Signal) -> float:
            try:
                return self.taxonomy.get(sig.signal_type).weight
            except Exception:
                return 0.0

        signals.sort(key=lambda s: (s.observed_at, weight(s)), reverse=True)
        if limit is not None:
            signals = signals[:limit]
        return signals

    def newest(self, domain: str, signal_type: str) -> Optional[Signal]:
        rows = self.for_account(domain, types=[signal_type], limit=1)
        return rows[0] if rows else None

    def counts_by_type(self, domain: str | None = None) -> dict[str, int]:
        if domain:
            rows = self.db.query(
                "SELECT signal_type, COUNT(*) AS n FROM signals WHERE domain = ? GROUP BY signal_type",
                (domain,),
            )
        else:
            rows = self.db.query(
                "SELECT signal_type, COUNT(*) AS n FROM signals GROUP BY signal_type"
            )
        return {r["signal_type"]: r["n"] for r in rows}

    def new_since(self, iso_ts: str, *, primary_only: bool = False) -> list[Signal]:
        rows = self.db.query(
            "SELECT * FROM signals WHERE first_seen_at >= ? ORDER BY first_seen_at DESC",
            (iso_ts,),
        )
        signals = [Signal.from_db_row(r) for r in rows]
        if primary_only:
            primary = self.taxonomy.primary_types()
            signals = [s for s in signals if s.signal_type in primary]
        return signals

    def purge_domain(self, domain: str) -> int:
        n = self.db.one("SELECT COUNT(*) AS n FROM signals WHERE domain = ?", (domain,))["n"]
        self.db.execute("DELETE FROM signals WHERE domain = ?", (domain,))
        self.db.execute("DELETE FROM play_assignments WHERE domain = ?", (domain,))
        return int(n)

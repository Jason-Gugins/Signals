"""Tests for CSV exports."""

from __future__ import annotations

import csv
from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Signal
from src.export.csvout import ACCOUNT_COLUMNS, PLAY_COLUMNS, SIGNAL_COLUMNS, export_accounts, export_all, export_signals
from src.identity.registry import AccountRegistry
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy


def test_headers_bom_roundtrip_empty_and_filters(tmp_path):
    db = Database(tmp_path / "s.db")
    empty = export_accounts(db, str(tmp_path / "empty.csv"))
    raw = Path(empty).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with Path(empty).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ACCOUNT_COLUMNS
    assert len(rows) == 1

    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme, \"Inc\"\nLtd", cohort="c1", tier=1, score=80))
    reg.upsert(Account(domain="beta.com", name="Beta", cohort="c2", tier=3, score=10))
    spec = Taxonomy.load().get("award")
    SignalStore(db).upsert(
        Signal(
            signal_id="s1", domain="acme.com", signal_type="award", category=spec.category,
            origin=spec.origin, catalyst=spec.catalyst, polarity=spec.polarity,
            observed_at="2026-08-01", source="news", confidence=0.9, first_seen_at="2026-08-01",
            last_seen_at="2026-08-01", evidence="hello, world",
        )
    )
    path = export_accounts(db, str(tmp_path / "acc.csv"), cohort="c1", tier_max=2)
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        recs = list(csv.DictReader(fh))
    assert len(recs) == 1
    assert "Acme" in recs[0]["name"]
    sig_path = export_signals(db, str(tmp_path / "sig.csv"))
    with Path(sig_path).open(encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh))
        recs = list(csv.DictReader(open(sig_path, encoding="utf-8-sig", newline="")))
    assert header == SIGNAL_COLUMNS
    assert recs[0]["evidence"] == "hello, world"
    paths = export_all(db, str(tmp_path / "all"))
    assert len(paths) == 3
    assert Path(paths[2]).read_text(encoding="utf-8-sig").splitlines()[0].split(",")[0] == PLAY_COLUMNS[0]

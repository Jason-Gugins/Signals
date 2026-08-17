from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Signal
from src.identity.registry import AccountRegistry
from src.pipeline.watchlist import add, check, import_closed_lost
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy


TODAY = date(2026, 8, 16)


def test_watchlist_alert_cooldown_and_import(tmp_path):
    db = Database(tmp_path / "s.db")
    tax = Taxonomy.load()
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme", tier=2, score=40))
    add(db, "acme.com", reason="closed_lost")
    store = SignalStore(db)
    spec = tax.get("funding_round")
    store.upsert(
        Signal(
            signal_id="f1", domain="acme.com", signal_type="funding_round",
            category=spec.category, origin=spec.origin, catalyst=spec.catalyst, polarity=spec.polarity,
            observed_at="2026-08-15", source="sec", confidence=0.9,
            first_seen_at="2026-08-15", last_seen_at="2026-08-15",
        )
    )
    alerts = check(db, taxonomy=tax, today=TODAY)
    assert len(alerts) == 1
    assert alerts[0].signal_type == "funding_round"
    assert check(db, taxonomy=tax, today=TODAY) == []  # cooldown
    add(db, "acme.com", reason="closed_lost", alert_on_types=["layoff"])
    store.upsert(
        Signal(
            signal_id="f2", domain="acme.com", signal_type="award",
            category="neutral", origin="internal", catalyst="secondary", polarity="neutral",
            observed_at="2026-08-16", source="n", confidence=0.9,
            first_seen_at="2026-08-16", last_seen_at="2026-08-16",
        )
    )
    assert check(db, taxonomy=tax, today=date(2026, 10, 1), cooldown_days=1) == []
    csv = tmp_path / "lost.csv"
    csv.write_text("domain,lost_date,reason,notes\nbeta.com,2026-01-01,no budget,later\n", encoding="utf-8")
    n = import_closed_lost(db, str(csv))
    assert n == 1
    assert reg.get("beta.com") is not None
    row = db.one("SELECT reason FROM watchlist WHERE domain='beta.com'")
    assert row["reason"] == "closed_lost"

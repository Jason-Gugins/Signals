"""Task 4: per-type candidate cap orders by (observed_at, relevance) so
rerank decides which candidates survive the keep-N cap."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.models import Account, Signal
from src.signals.score import score_account
from src.signals.taxonomy import Taxonomy

TODAY = date(2026, 8, 16)
TAX = Taxonomy.load(str(Path("config/signals.yaml")))
CFG = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))
ACCT = Account(domain="acme.com", name="Acme", icp_fit=1.0)


def _sig(sid, typ, observed, source="sec", conf=1.0, **kw) -> Signal:
    spec = TAX.get(typ)
    return Signal(
        signal_id=sid,
        domain="acme.com",
        signal_type=typ,
        category=spec.category,
        origin=spec.origin,
        catalyst=spec.catalyst,
        polarity=spec.polarity,
        observed_at=observed,
        source=source,
        confidence=conf,
        **kw,
    )


def test_cap_tie_broken_by_relevance():
    """Same observed_at, cap keeps 3 of 4: the 0.9-relevance candidate
    (last in input order) must survive; the irrelevant first one drops."""
    sigs = [
        _sig(f"f{i}", "funding_round", "2026-08-16", evidence_data={"relevance": rel})
        for i, rel in enumerate([0.2, 0.4, 0.5, 0.9])
    ]
    res = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    kept = {c.signal_id for c in res.contributions if c.dropped_reason is None}
    assert "f3" in kept  # highest relevance wins the tie
    assert "f0" not in kept  # lowest relevance loses it


def test_cap_without_relevance_unchanged():
    """Relevance absent -> ordering identical to today (stable by input order
    on observed_at ties)."""
    sigs = [
        _sig(f"f{i}", "funding_round", "2026-08-16") for i in range(4)
    ]
    res = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    kept = {c.signal_id for c in res.contributions if c.dropped_reason is None}
    assert kept == {"f0", "f1", "f2"}
    dropped = {c.signal_id for c in res.contributions if c.dropped_reason == "per_type_cap"}
    assert dropped == {"f3"}

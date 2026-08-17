"""Tests for decay-weighted account scoring."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import yaml

from src.core.models import Account, Signal
from src.signals.score import decay_factor, score_account
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


def test_decay_at_boundaries():
    floor = CFG["decay"]["floor"]
    assert decay_factor("2026-08-16", TODAY, 180, floor) == 1.0
    assert abs(decay_factor("2026-02-17", TODAY, 180, floor) - 0.5) < 0.02  # ~180d
    half = decay_factor("2025-08-22", TODAY, 180, floor)
    assert abs(half - 0.25) < 0.05 or half == floor or half <= 0.26
    assert decay_factor("2010-01-01", TODAY, 10, floor) == floor
    assert decay_factor("2026-12-01", TODAY, 180, floor) == 1.0


def test_per_type_cap_keeps_newest_3():
    sigs = [
        _sig(f"f{i}", "funding_round", f"2026-08-{16-i:02d}")
        for i in range(5)
    ]
    res = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    dropped = [c for c in res.contributions if c.dropped_reason == "per_type_cap"]
    kept = [c for c in res.contributions if c.dropped_reason is None]
    assert len(dropped) == 2
    assert len(kept) == 3
    assert {c.signal_id for c in kept} == {"f0", "f1", "f2"}


def test_drop_superseded_low_conf_unknown():
    sigs = [
        _sig("a", "funding_round", "2026-08-01", superseded_by="x"),
        _sig("b", "funding_round", "2026-08-01", conf=0.1),
        Signal(
            signal_id="c", domain="acme.com", signal_type="not_real",
            category="x", origin="internal", catalyst="primary", polarity="positive",
            observed_at="2026-08-01", source="x", confidence=0.9,
        ),
    ]
    res = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    reasons = {c.dropped_reason for c in res.contributions}
    assert "superseded" in reasons
    assert "low_confidence" in reasons
    assert "unknown_type" in reasons
    assert all(c.value == 0 or c.dropped_reason for c in res.contributions)


def test_per_source_cap_scales():
    heavy = [_sig(f"h{i}", "funding_round", "2026-08-10", source="sec") for i in range(3)]
    light = [_sig("l1", "award", "2026-08-10", source="news")]
    nocap = dict(CFG)
    nocap["caps"] = {**CFG["caps"], "per_source_max_share": 1.0}
    uncapped = score_account(ACCT, heavy + light, taxonomy=TAX, cfg=nocap, today=TODAY, combos=[])
    capped = score_account(ACCT, heavy + light, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    def src_sum(res, src):
        return sum(c.value for c in res.contributions if not c.dropped_reason and c.source == src)
    u_sec, u_tot = src_sum(uncapped, "sec"), sum(c.value for c in uncapped.contributions if not c.dropped_reason)
    c_sec = src_sum(capped, "sec")
    assert u_sec / u_tot > 0.5
    assert abs(c_sec - CFG["caps"]["per_source_max_share"] * u_tot) < 1e-6


def test_combos_before_sat_icp_after():
    sigs = [_sig("f1", "funding_round", "2026-08-01")]
    combo = [{"id": "x", "bonus": 25, "urgency": 9, "action": "go"}]
    base = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    with_c = score_account(ACCT, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=combo)
    assert with_c.raw > base.raw
    assert with_c.urgency == 9
    hot = Account(domain="acme.com", icp_fit=2.0)
    scaled = score_account(hot, sigs, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    assert scaled.raw == base.raw * 2.0
    assert scaled.score > base.score


def test_score_monotonic_capped_empty_deterministic():
    empty = score_account(ACCT, [], taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    assert empty.score == 0.0 and empty.contributions == []
    a = [_sig("a", "funding_round", "2026-08-01")]
    b = a + [_sig("b", "layoff", "2026-08-01")]
    sa = score_account(ACCT, a, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    sb = score_account(ACCT, b, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    assert sb.score >= sa.score
    assert sa.score <= 100 and sb.score <= 100
    again = score_account(ACCT, a, taxonomy=TAX, cfg=CFG, today=TODAY, combos=[])
    assert again.score == sa.score
    assert again.to_components_json() == sa.to_components_json()
    k = CFG["saturation"]["k"]
    expected = round(100 * (1 - math.exp(-sa.raw / k)), 1)
    assert sa.score == expected

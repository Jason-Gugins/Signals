"""Regression: marketplace_review_trend must persist (was rejected as unknown).

The P1 review-trend feature emitted a signal type absent from signals.yaml —
normalize_candidate raised unknown_signal_type and every delta was discarded
while stats_state still advanced, silently burning the trend data. This test
pins the taxonomy entry so the type can never regress to unknown.
"""

from __future__ import annotations

from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.marketplace.trend import review_trend_signal


def test_review_trend_signal_persists_through_normalize():
    tax = Taxonomy.load()
    assert "marketplace_review_trend" in tax._types, (
        "marketplace_review_trend missing from signals.yaml — review-trend "
        "signals are being rejected as unknown and never persist"
    )
    cand = review_trend_signal(
        "acme",
        "g2",
        prev={"count": 10, "avg_rating": 4.0},
        curr={"count": 25, "avg_rating": 4.0},
        domain="acme.com",
        today="2026-09-03",
    )
    assert cand is not None
    from src.core.models import Account

    valid, rejected = normalize_batch(
        [cand],
        account=Account(domain="acme.com", name="Acme"),
        source="g2",
        taxonomy=tax,
        now="2026-09-03T00:00:00Z",
    )
    assert len(valid) == 1
    assert rejected == []
    assert valid[0].signal_type == "marketplace_review_trend"

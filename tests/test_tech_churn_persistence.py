"""Regression: tech_churn must persist (was rejected as unknown).

The P1 tech-change-detection feature emitted `tech_churn` from
`diff_technologies`, but the type was absent from signals.yaml —
normalize_candidate raised unknown_signal_type and every churn candidate was
silently discarded while the technologies table still advanced, burning the
churn signal on every cycle. This test pins the taxonomy entry so the type
can never regress to unknown (same shape as test_review_trend_persistence).
"""

from __future__ import annotations

from src.core.models import Account
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.techstack.diff import diff_technologies


def test_tech_churn_persists_through_normalize():
    tax = Taxonomy.load()
    assert "tech_churn" in tax._types, (
        "tech_churn missing from signals.yaml — tech-churn signals are being "
        "rejected as unknown and never persist"
    )
    changes = diff_technologies(
        {"hubspot", "salesforce"},
        {"salesforce"},
        domain="acme.com",
        today="2026-09-04",
    )
    churn = [cand for stype, cand in changes if stype == "tech_churn"]
    assert churn, "expected a churn candidate for the removed vendor"
    assert churn[0].natural_key == "techchg:acme.com:hubspot:2026-09-04"

    valid, rejected = normalize_batch(
        churn,
        account=Account(domain="acme.com", name="Acme"),
        source="techstack",
        taxonomy=tax,
        now="2026-09-04T00:00:00Z",
    )
    assert len(valid) == 1
    assert not rejected

from datetime import date
from src.sources.repvue_db.collector import repvue_to_candidates


def test_repvue_thresholds():
    today = date(2026, 8, 16)
    row = {"domain": "acme.com", "repvue_score": 70, "quota_attainment": 0.40, "hiring": 1}
    prior = {"repvue_score": 80, "quota_attainment": 0.55, "hiring": 0}
    cands = repvue_to_candidates(row, prior, today=today)
    types = {c.signal_type for c in cands}
    assert "stagnation" in types
    assert "hiring_surge" in types
    assert any(c.confidence == 0.6 for c in cands)
    assert repvue_to_candidates(row, None, today=today) == []

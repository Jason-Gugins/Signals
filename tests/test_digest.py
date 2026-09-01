"""Tests for alert digest rendering (Task 21)."""

from __future__ import annotations

from src.core.models import Signal
from src.export.digest import build_digest
from src.signals.plays import PlayAssignment
from src.signals.taxonomy import Taxonomy


def _sig(signal_type: str = "funding_round", domain: str = "acme.com", **kw) -> Signal:
    defaults = dict(
        signal_id=f"{domain}-{signal_type}-1",
        domain=domain,
        signal_type=signal_type,
        category="test",
        origin="test",
        catalyst="catalyst",
        polarity="positive",
        observed_at="2026-08-30",
        source="test",
        title=f"{signal_type} title",
        summary=None,
        evidence=f"evidence for {signal_type}",
        evidence_data={},
    )
    defaults.update(kw)
    return Signal(**defaults)


def _play(play_id: str = "p1") -> PlayAssignment:
    return PlayAssignment(
        play_id=play_id,
        play_name=f"Play {play_id}",
        signal_id="sig-1",
        rank=1,
        urgency=1,
        variables={},
        opener=f"opener {play_id}",
        t24=f"t24 {play_id}",
        cta="cta",
        loss_aversion="loss",
    )


def _tax() -> Taxonomy:
    return Taxonomy.load()


def test_groups_by_signal_type():
    sigs = [
        _sig("funding_round"),
        _sig("exec_hire"),
        _sig("funding_round", signal_id="s3"),
    ]
    doc = build_digest("acme.com", sigs, [], period="weekly", taxonomy=_tax())
    labels = [_tax().get("funding_round").label, _tax().get("exec_hire").label]
    assert all(lbl in doc for lbl in labels)
    # each group shows its count
    assert "(2)" in doc
    assert "(1)" in doc


def test_why_now_from_evidence_data():
    sig = _sig("funding_round", evidence_data={"amount_usd": 40_000_000, "stage": "Series B"})
    doc = build_digest("acme.com", [sig], [], period="daily", taxonomy=_tax())
    assert "raised" in doc.lower()
    assert "40" in doc
    assert "Series B" in doc


def test_why_now_falls_back_to_evidence():
    sig = _sig("exec_hire", evidence="plain evidence string")
    doc = build_digest("acme.com", [sig], [], period="daily", taxonomy=_tax())
    assert "plain evidence string" in doc


def test_empty_state_doc():
    doc = build_digest("acme.com", [], [], period="daily", taxonomy=_tax())
    assert doc.strip()
    assert "# " in doc
    assert "No signals this period" in doc


def test_top_plays_section():
    plays = [_play("p1"), _play("p2"), _play("p3")]
    doc = build_digest("acme.com", [_sig("funding_round")], plays, period="daily", taxonomy=_tax())
    assert "opener p1" in doc
    assert "t24 p1" in doc
    assert "opener p2" in doc
    assert "opener p3" not in doc  # only first 2


def test_period_label_in_header():
    for period in ("daily", "weekly"):
        doc = build_digest("acme.com", [], [], period=period, taxonomy=_tax())
        assert period in doc


def test_date_range_in_header():
    sigs = [_sig("funding_round", observed_at="2026-08-28"), _sig("exec_hire", observed_at="2026-08-30")]
    doc = build_digest("acme.com", sigs, [], period="weekly", taxonomy=_tax())
    assert "2026-08-28" in doc
    assert "2026-08-30" in doc


def test_write_digest_file(tmp_path):
    from src.cli import write_digest

    p = write_digest(str(tmp_path / "acme.com.md"), "# hello")
    assert p.endswith("acme.com.md")
    assert (tmp_path / "acme.com.md").read_text(encoding="utf-8") == "# hello"

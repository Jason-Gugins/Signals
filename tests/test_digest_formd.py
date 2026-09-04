"""Form D unmatched-issuer section in account digests.

Stub accounts (cikXXXXXXXXXX.edgar) hold funding_form_d signals for issuers
that matched no real account in the registry — this section surfaces them so
newly-funded companies don't evaporate.
"""

from __future__ import annotations

from src.core.models import Signal
from src.export.digest import FORMD_SECTION_TITLE, build_digest


def _stub_sig(**kw) -> Signal:
    """A funding_form_d signal on a Form D stub-account domain."""
    domain = kw.pop("domain", "cik0001791983.edgar")
    defaults = dict(
        signal_id=f"{domain}-formd-1",
        domain=domain,
        signal_type="funding_form_d",
        category="funding",
        origin="sec_formd",
        catalyst="form_d",
        polarity="positive",
        observed_at="2026-09-02",
        source="sec_formd",
        title="Acme Form D Series A",
        summary=None,
        evidence=None,
        evidence_data={
            "entity_name": "Acme Labs Inc",
            "amount_usd": 12_000_000,
            "state": "CA",
            "cik": "0001791983",
        },
    )
    defaults.update(kw)
    return Signal(**defaults)


def _doc(sigs, **kw):
    return build_digest("global", sigs, [], period="daily", include_formd_unmatched=True, **kw)


def test_stub_signal_appears_with_fields():
    sig = _stub_sig()
    doc = _doc([sig])
    assert f"## {FORMD_SECTION_TITLE}" in doc
    line = [ln for ln in doc.splitlines() if ln.startswith("- Acme Labs Inc")][0]
    assert "$12M" in line
    assert "CA" in line
    assert "filed 2026-09-02" in line
    assert "CIK 0001791983" in line


def test_non_stub_formd_signal_excluded():
    sig = _stub_sig(domain="realco.com", signal_id="realco-formd-1")
    doc = _doc([sig])
    assert FORMD_SECTION_TITLE not in doc
    # The signal itself still renders in its normal signal-type group.
    assert "funding_form_d" in doc or "Form D" in doc


def test_no_stubs_section_omitted():
    sig = _stub_sig(signal_type="funding_round", domain="other.com", signal_id="x1")
    doc = _doc([sig])
    assert FORMD_SECTION_TITLE not in doc
    assert "## Top plays" in doc  # rest of digest unaffected


def test_default_render_off():
    """Per-domain renders (default kwarg) never include the section."""
    sig = _stub_sig()
    doc = build_digest("cik0001791983.edgar", [sig], [], period="daily")
    assert FORMD_SECTION_TITLE not in doc


def test_cap_at_25_highest_amounts():
    sigs = [
        _stub_sig(
            domain=f"cik{i:010d}.edgar",
            signal_id=f"s{i}",
            evidence_data={
                "entity_name": f"Issuer {i}",
                "amount_usd": 1_000 * (i + 1),
                "state": "NY",
                "cik": f"{i:010d}",
            },
        )
        for i in range(27)
    ]
    doc = _doc(sigs)
    section = doc.split(f"## {FORMD_SECTION_TITLE}")[1].split("## Top plays")[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert len(rows) == 25
    # Sorted by amount desc: the two smallest (Issuer 0, Issuer 1) are cut.
    assert "Issuer 26" in doc
    assert "Issuer 2" in doc
    assert "- Issuer 1 " not in section
    assert "- Issuer 0 " not in section


def test_sorted_by_amount_desc():
    low = _stub_sig(
        domain="cik0000000001.edgar",
        signal_id="low",
        evidence_data={"entity_name": "Low Co", "amount_usd": 500_000, "state": "TX", "cik": "0000000001"},
    )
    high = _stub_sig(
        domain="cik0000000002.edgar",
        signal_id="high",
        evidence_data={"entity_name": "High Co", "amount_usd": 9_000_000, "state": "WA", "cik": "0000000002"},
    )
    doc = _doc([low, high])
    section = doc.split(f"## {FORMD_SECTION_TITLE}")[1].split("## Top plays")[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert rows[0].startswith("- High Co")
    assert rows[1].startswith("- Low Co")


def test_missing_evidence_graceful():
    """Stub signal with bare evidence_data still renders a row, not a crash."""
    sig = _stub_sig(signal_id="bare", evidence_data={})
    doc = _doc([sig])
    assert f"## {FORMD_SECTION_TITLE}" in doc


def test_digest_paths_wiring_passes_formd_flag(monkeypatch, tmp_path):
    """Parent wiring: _digest_paths must pass include_formd_unmatched=True."""
    from datetime import date
    import src.cli as cli
    import src.export.digest as digest_mod
    import src.signals.plays as plays_mod
    import src.signals.tier as tier_mod
    import src.pipeline.orchestrator as orch_mod
    from src.core.models import Account
    from src.signals.tier import TierResult
    from types import SimpleNamespace

    TODAY = date(2026, 9, 3)
    captured = {}

    def fake_build_digest(domain, signals, plays, *, period, taxonomy=None,
                          since=None, include_formd_unmatched=False):
        captured["include_formd_unmatched"] = include_formd_unmatched
        return f"# {domain} digest"

    monkeypatch.setattr(digest_mod, "build_digest", fake_build_digest)
    monkeypatch.setattr(tier_mod, "assign_tier",
                        lambda signals, result, **kw: TierResult(1, "active", "ok."))
    monkeypatch.setattr(plays_mod, "assign_plays", lambda *a, **kw: [])
    monkeypatch.setattr("src.signals.score.score_account", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr("src.signals.calibration.load_stats", lambda db: {})
    monkeypatch.setattr(orch_mod, "_today", lambda: TODAY)
    monkeypatch.setattr(cli, "_today", lambda: TODAY, raising=False)

    class _Store:
        def for_account(self, domain):
            return []

    class _FakeOrch:
        db = object()
        signal_store = _Store()
        taxonomy = None

        def _accounts(self, *, domains=None, cohort=None, **kw):
            return [Account(domain="cik0002039747.edgar", name="Test Issuer")]

        def _contacts(self, domain):
            return []

    class _Storage:
        def __init__(self, d):
            self.digests_dir = str(d)

    class _Cfg:
        def load_yaml(self, name):
            return {}

    cfg = _Cfg()
    cfg.storage = _Storage(tmp_path)
    ctx = SimpleNamespace(obj={"get_orch": lambda: _FakeOrch(), "config": cfg, "cohort": None})
    cli._digest_paths(ctx, "daily", ())
    assert captured["include_formd_unmatched"] is True

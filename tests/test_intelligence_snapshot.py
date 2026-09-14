"""Tests for the authoritative intelligence snapshot (Task 9).

The snapshot is the single calculation the dossier consumes: one ``today``,
one account, and expired signals excluded from score/tier/plays.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.config import Config
from src.core.models import Account, Contact, Document, Signal
from src.identity.icp import evaluate_icp
from src.intel.snapshot import IntelligenceSnapshot, build_intelligence_snapshot
from src.pipeline.orchestrator import Orchestrator
from src.signals.lifecycle import load_supersede_map
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter


TODAY = date(2026, 8, 16)
TAX = Taxonomy.load("config/signals.yaml")
SIGNALS_CFG = yaml.safe_load(Path("config/signals.yaml").read_text(encoding="utf-8"))
SUPERSEDE = load_supersede_map(SIGNALS_CFG)
SCORING = yaml.safe_load(Path("config/scoring.yaml").read_text(encoding="utf-8"))
PLAYS = yaml.safe_load(Path("config/plays.yaml").read_text(encoding="utf-8"))


def _sig(sid: str, typ: str, observed: str, *, conf: float = 0.9, **kw) -> Signal:
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
        source="sec_edgar",
        confidence=conf,
        **kw,
    )


def test_snapshot_partitions_active_and_expired():
    # hiring_surge declares supersede_days: 30 in config/signals.yaml.
    assert SUPERSEDE.get("hiring_surge") == 30
    old = _sig("old-hiring", "hiring_surge", "2026-07-02")  # 45 days before TODAY
    fresh = _sig("fresh-hiring", "hiring_surge", "2026-08-11")  # 5 days before TODAY
    account = Account(domain="acme.com", name="Acme", icp_fit=1.0)
    # A combo whose window (90d) is wider than the supersede window (30d):
    # without partitioning, the 45-day-old signal would match it.
    scoring_cfg = {
        **SCORING,
        "combos": [
            {
                "id": "probe_hiring",
                "bonus": 1,
                "urgency": 1,
                "action": "probe",
                "all_of": [{"any_type": ["hiring_surge"], "within_days": 90}],
            }
        ],
    }
    snap = build_intelligence_snapshot(
        account=account,
        signals=[old, fresh],
        taxonomy=TAX,
        scoring_cfg=scoring_cfg,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    assert [s.signal_id for s in snap.active_signals] == ["fresh-hiring"]
    assert [s.signal_id for s in snap.expired_signals] == ["old-hiring"]

    contrib_ids = {c.signal_id for c in snap.score.contributions}
    assert "fresh-hiring" in contrib_ids
    assert "old-hiring" not in contrib_ids

    matched = {sid for combo in snap.combos for sid in combo.get("matched_signal_ids", [])}
    assert "fresh-hiring" in matched
    assert "old-hiring" not in matched


def test_snapshot_components_share_one_today():
    fresh = _sig("f1", "funding_round", "2026-08-10", evidence_data={"round_stage": "Series B"})
    account = Account(domain="acme.com", name="Acme", icp_fit=1.0)
    snap = build_intelligence_snapshot(
        account=account,
        signals=[fresh],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    assert isinstance(snap, IntelligenceSnapshot)
    assert snap.today == TODAY
    # Tier's buying window is date-derived, so a populated value proves the
    # tier was produced for TODAY (not the real clock).
    assert snap.tier.buying_window
    assert isinstance(snap.score.score, float)
    assert snap.combos == tuple(snap.combos)
    assert snap.plays == tuple(snap.plays)
    # The combos stored on the snapshot are exactly what one call for TODAY yields.
    from src.signals.combos import evaluate_combos

    assert list(snap.combos) == evaluate_combos(
        list(snap.active_signals), SCORING.get("combos") or [], today=TODAY
    )


def test_snapshot_fit_reflects_disqualifying_rules():
    fresh = _sig("f1", "funding_round", "2026-08-10")
    account = Account(domain="acme.com", name="Acme Inc", icp_fit=1.0)
    rules = {
        "disqualifiers": [
            {"id": "competitor", "when": {"name_regex": "Acme"}, "reason": "Competitor"}
        ]
    }
    snap = build_intelligence_snapshot(
        account=account,
        signals=[fresh],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules=rules,
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    assert snap.fit is not None
    assert snap.fit.disqualified is True
    assert snap.fit.multiplier == 0.0

    # No rules -> no fit object at all.
    no_fit = build_intelligence_snapshot(
        account=account,
        signals=[fresh],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    assert no_fit.fit is None


def test_persona_framing_is_nullable_text_not_a_bucket():
    fresh = _sig("f1", "funding_round", "2026-08-10")
    account = Account(domain="acme.com", name="Acme", icp_fit=1.0)

    no_contacts = build_intelligence_snapshot(
        account=account,
        signals=[fresh],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    assert no_contacts.persona_framing is None

    contact = Contact(person_key="jane", domain="acme.com", name="Jane Doe", title="VP Revenue")
    with_contact = build_intelligence_snapshot(
        account=account,
        signals=[fresh],
        taxonomy=TAX,
        scoring_cfg=SCORING,
        plays_cfg=PLAYS,
        icp_rules={},
        contacts=[contact],
        today=TODAY,
        supersede_days_by_type=SUPERSEDE,
    )
    framing = with_contact.persona_framing
    assert isinstance(framing, str) and framing.strip()
    assert framing not in {"revenue", "tech", "exec"}


class _LocalAdapter(SourceAdapter):
    key = "local_ok"
    tier = "http"
    cadence_hours = 1

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://local.test/{account.domain}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return [SignalCandidate("award", "2026-08-01", f"aw:{account.domain}", title="Award")]


class _FakeFetch:
    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        doc = Document(
            doc_id=f"doc-{task.domain}", source=task.source, url=task.url,
            domain=task.domain, body=b"ok", status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _orch(tmp_path) -> Orchestrator:
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=_FakeFetch(), adapters=[_LocalAdapter()])


def _seed_one(tmp_path) -> Orchestrator:
    """Reuse the tests/test_orchestrator.py seeding pattern (csv -> seed -> collect)."""
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    orch = _orch(tmp_path)
    orch.seed(csv=str(csv_path), linkedin=False, repvue=False, cohort=None)
    orch.collect(force=True)
    assert orch.db.one("SELECT COUNT(*) AS n FROM signals WHERE domain='acme.com'")["n"] >= 1
    return orch


def test_score_returns_snapshots_only_on_request(tmp_path):
    orch = _seed_one(tmp_path)

    plain = orch.score(domains=["acme.com"])
    assert "snapshots" not in plain
    assert plain["scored"] == 1

    with_snaps = orch.score(domains=["acme.com"], return_snapshots=True)
    assert with_snaps["scored"] == 1
    assert "snapshots" in with_snaps
    assert "acme.com" in with_snaps["snapshots"]
    assert isinstance(with_snaps["snapshots"]["acme.com"], IntelligenceSnapshot)


def test_snapshot_score_matches_persisted_score(tmp_path):
    orch = _seed_one(tmp_path)

    out = orch.score(domains=["acme.com"], return_snapshots=True)
    snap = out["snapshots"]["acme.com"]
    acct = orch.registry.get("acme.com")
    # The dossier reads the snapshot; the registry row is the persisted truth.
    assert snap.score.score == acct.score
    assert snap.tier.tier == acct.tier
    assert snap.tier.buying_window == acct.buying_window
    hist = orch.db.query(
        "SELECT score, tier FROM score_history WHERE domain='acme.com' ORDER BY as_of DESC LIMIT 1"
    )
    assert hist and hist[0]["score"] == snap.score.score
    assert hist[0]["tier"] == snap.tier.tier
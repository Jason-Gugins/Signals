"""Seed-time ICP scoring (Task 15) + score-time multiplier-path verification.

What was verified end-to-end BEFORE writing these tests (read from source,
not assumed):

- Score-time entry: ``Orchestrator.resolve()`` (src/pipeline/orchestrator.py
  lines 114-129) loads rules via ``self.config.load_yaml("icp")`` (line 116),
  calls ``evaluate_icp(acct, rules, signals=self.signal_store.for_account(
  acct.domain))`` (line 121), and persists ``acct.icp_fit = result.multiplier``
  (line 122) through ``registry.upsert`` (line 126) — the evaluate_icp
  multiplier path IS exercised at score time today.
- Consumption: ``Orchestrator.score()`` (orchestrator.py line 386) feeds the
  account into ``score_account``; src/signals/score.py line 181 reads
  ``account.icp_fit`` (falling back to 1.0 when None) and applies
  ``raw *= icp`` (line 182), exposed as ``ScoreResult.icp_multiplier``
  (line 195). ``score()`` never recomputes ICP — it reads the stored value
  off the account row loaded from the DB.
- Storage: ``accounts.icp_fit REAL`` + ``icp_reasons TEXT`` already exist
  (src/core/db.py lines 45-46; src/core/models.py lines 71-72, 110-111,
  124-128) — no model or schema change was needed for this task.

What these tests prove:

1. Seeding with config/icp.yaml rules (passed in by the orchestrator) runs
   ``evaluate_icp`` once per account after ``registry.upsert`` and persists
   ``icp_fit``/``icp_reasons`` on the account row.
2. Seeding without icp.yaml (missing file OR empty YAML -> ``{}``) leaves the
   1.0 default with no ICP write.
3. A disqualifier rule persists multiplier 0.0 (plus
   ``disqualified``/``disqualify_reason``).
4. The score path uses the STORED value: ``score_account`` reports
   ``icp_multiplier ==`` the seeded fit and scales raw accordingly, and a full
   ``orchestrator.score()`` run writes that multiplier into the
   ``score_history`` components JSON — i.e. the seed-time value flows through
   the score-time multiplier path unchanged.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account, Signal
from src.identity.registry import AccountRegistry
from src.identity.seeds import seed_from_csv
from src.pipeline.orchestrator import Orchestrator
from src.signals.score import score_account
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"

RULES = {
    "rules": [
        {
            "id": "size_sweet_spot",
            "when": {"employee_count_between": [50, 2000]},
            "multiplier": 1.25,
            "reason": "Headcount in ICP band",
        },
        {
            "id": "geo",
            "when": {"hq_country_any": ["United States"]},
            "multiplier": 1.1,
            "reason": "Serviceable geography",
        },
    ],
    "disqualifiers": [],
}

DISQ_RULES = {
    "rules": [],
    "disqualifiers": [
        {"id": "blocked", "when": {"name_regex": "Blocked"}, "reason": "Competitor"}
    ],
}

SEED_CSV = "domain,name,industry,employee_count,hq_country\ngong.io,Gong,Software,1200,United States\n"
DISQ_CSV = "domain,name,industry,employee_count\ngong.io,Blocked Corp,Software,1200\n"

SCORING_YAML = """decay: {half_life_days: 180, floor: 0.02}
caps: {per_type_max_signals: 3, per_source_max_share: 0.5}
saturation: {k: 40.0}
icp: {min_multiplier: 0.0, max_multiplier: 2.0}
combos: []
buying_window: {active_days: 30, opening_days: 90, developing_days: 180}
tiers: {}
"""


def _registry(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "signals.db"))


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _orch(tmp_path, *, with_icp: bool) -> Orchestrator:
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "signals.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = str(tmp_path / "config")
    if with_icp:
        _write_yaml(tmp_path / "config" / "icp.yaml", RULES)
    _write_yaml(tmp_path / "config" / "scoring.yaml", yaml.safe_load(SCORING_YAML))
    _write_yaml(tmp_path / "config" / "plays.yaml", {"plays": {}})
    return Orchestrator(cfg)


def test_seed_with_rules_persists_icp_fit(tmp_path):
    reg = _registry(tmp_path)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")

    stats = seed_from_csv(reg, csv_path, icp_rules=RULES)

    assert stats.created == 1
    row = reg.get("gong.io")
    assert row.icp_fit == pytest.approx(1.375)
    assert sorted(row.icp_reasons) == ["Headcount in ICP band", "Serviceable geography"]
    assert row.disqualified is False
    assert row.disqualify_reason is None


def test_seed_without_rules_keeps_default(tmp_path):
    reg = _registry(tmp_path)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")

    # No rules passed (orchestrator could not load icp.yaml -> passes None/{})
    seed_from_csv(reg, csv_path)
    row = reg.get("gong.io")
    assert row.icp_fit == 1.0
    assert row.icp_reasons == []

    # Empty rules dict behaves identically: no ICP write, default kept
    seed_from_csv(reg, csv_path, icp_rules={})
    row = reg.get("gong.io")
    assert row.icp_fit == 1.0
    assert row.icp_reasons == []


def test_orchestrator_seed_without_icp_yaml_keeps_default(tmp_path):
    orch = _orch(tmp_path, with_icp=False)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")

    orch.seed(csv=str(csv_path))

    row = orch.registry.get("gong.io")
    assert row.icp_fit == 1.0
    assert row.icp_reasons == []


def test_orchestrator_seed_with_empty_icp_yaml_keeps_default(tmp_path):
    orch = _orch(tmp_path, with_icp=False)
    _write_yaml(tmp_path / "config" / "icp.yaml", {})  # present but empty
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")

    orch.seed(csv=str(csv_path))

    row = orch.registry.get("gong.io")
    assert row.icp_fit == 1.0
    assert row.icp_reasons == []


def test_seed_disqualifier_persists_zero_multiplier(tmp_path):
    reg = _registry(tmp_path)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(DISQ_CSV, encoding="utf-8")

    seed_from_csv(reg, csv_path, icp_rules=DISQ_RULES)

    row = reg.get("gong.io")
    assert row.icp_fit == 0.0
    assert row.disqualified is True
    assert row.disqualify_reason == "Competitor"


def test_reseed_without_rules_preserves_computed_icp_fit(tmp_path):
    """MAJOR regression: the first registry.upsert wrote Account's 1.0 default
    icp_fit with COALESCE, so re-seeding the same domain clobbered a
    previously computed ICP fit (1.375 -> 1.0) on every re-seed."""
    reg = _registry(tmp_path)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")

    stats = seed_from_csv(reg, csv_path, icp_rules=RULES)
    assert stats.created == 1
    row = reg.get("gong.io")
    assert row.icp_fit == pytest.approx(1.375)
    assert sorted(row.icp_reasons) == ["Headcount in ICP band", "Serviceable geography"]

    # Re-seed WITHOUT rules: the computed fit must survive untouched.
    stats2 = seed_from_csv(reg, csv_path)
    assert stats2.updated == 1
    row2 = reg.get("gong.io")
    assert row2.icp_fit == pytest.approx(1.375)
    assert sorted(row2.icp_reasons) == ["Headcount in ICP band", "Serviceable geography"]

    # And with empty rules dict — same guarantee.
    seed_from_csv(reg, csv_path, icp_rules={})
    row3 = reg.get("gong.io")
    assert row3.icp_fit == pytest.approx(1.375)


def test_score_path_uses_stored_icp_fit(tmp_path):
    orch = _orch(tmp_path, with_icp=True)
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(SEED_CSV, encoding="utf-8")
    orch.seed(csv=str(csv_path))

    acct = orch.registry.get("gong.io")
    assert acct.icp_fit == pytest.approx(1.375)  # stored by the seed path

    tax = Taxonomy.load(str(REPO_CONFIG / "signals.yaml"))
    spec = tax.get("award")
    SignalStore(orch.db, tax).upsert(
        Signal(
            signal_id="sig-award-1",
            domain="gong.io",
            signal_type="award",
            category=spec.category,
            origin=spec.origin,
            catalyst=spec.catalyst,
            polarity=spec.polarity,
            observed_at="2026-08-30",
            source="test",
            confidence=1.0,
        )
    )
    signals = orch.signal_store.for_account("gong.io")

    cfg = yaml.safe_load((tmp_path / "config" / "scoring.yaml").read_text(encoding="utf-8"))
    today = date(2026, 8, 31)
    stored = score_account(acct, signals, taxonomy=tax, cfg=cfg, today=today, combos=[])
    neutral = score_account(
        Account(domain="gong.io", icp_fit=1.0), signals, taxonomy=tax, cfg=cfg, today=today, combos=[]
    )
    # score_account reads account.icp_fit (score.py:181) and multiplies raw (score.py:182)
    assert stored.icp_multiplier == pytest.approx(1.375)
    assert stored.raw == pytest.approx(neutral.raw * 1.375)
    assert stored.score > neutral.score

    # Full orchestrator score() run: the stored value flows into score_history
    orch.score()
    hist = orch.db.one("SELECT components FROM score_history WHERE domain='gong.io'")
    components = json.loads(hist["components"])
    assert components["icp_multiplier"] == pytest.approx(1.375)

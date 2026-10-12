"""RetrievalConfig parsing: ships enabled (A/B-validated); a missing block behaves as off.

The shipped config/default.yaml flips retrieval ON (2026-10 A/B split test,
data/probe/RETRIEVAL_AB_2026_10.md — identical G4/G5 outcomes at 75-79% lower
Jev gate load), so these pins load the real default.yaml. The DATACLASS
defaults stay disabled: an old config file whose yaml lacks the retrieval
block still behaves as off (pinned below).

Mirrors the config-loader bootstrap in tests/test_config.py (isolated env_path
so the real .env never pollutes these assertions).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.config import Config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = ROOT / "config" / "default.yaml"


def test_retrieval_config_defaults_on(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")

    cfg = Config.load(yaml_path=DEFAULT_YAML, env_path=env_path)

    assert cfg.retrieval.enabled is True
    assert cfg.retrieval.candidate_limit == 180
    assert cfg.retrieval.stage_docs == 35
    assert cfg.retrieval.min_index_coverage == 0.8


def test_retrieval_config_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("SIGNALS_DB_PATH", raising=False)
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")

    override = tmp_path / "override.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "retrieval": {
                    "enabled": True,
                    "chunk_tokens": 400,
                    "chunk_overlap_tokens": 50,
                    "candidate_limit": 80,
                    "rrf_k": 30,
                    "rerank_top_n": 16,
                    "rerank_floor": 0.1,
                    "mmr_lambda": 0.5,
                    "stage_docs": 20,
                    "min_index_coverage": 0.6,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(yaml_path=override, env_path=env_path)

    assert cfg.retrieval.enabled is True
    assert cfg.retrieval.chunk_tokens == 400
    assert cfg.retrieval.chunk_overlap_tokens == 50
    assert cfg.retrieval.candidate_limit == 80
    assert cfg.retrieval.rrf_k == 30
    assert cfg.retrieval.rerank_top_n == 16
    assert cfg.retrieval.rerank_floor == 0.1
    assert cfg.retrieval.mmr_lambda == 0.5
    assert cfg.retrieval.stage_docs == 20
    assert cfg.retrieval.min_index_coverage == 0.6

    # A MISSING block parses to the disabled default — old config files
    # (written before this block shipped) behave as off.
    old = tmp_path / "old.yaml"
    old.write_text(
        yaml.safe_dump({"storage": {"db_path": "data/signals.db"}}),
        encoding="utf-8",
    )
    cfg2 = Config.load(yaml_path=old, env_path=env_path)
    assert cfg2.retrieval.enabled is False
    assert cfg2.retrieval.stage_docs == 35

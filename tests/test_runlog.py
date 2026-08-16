"""Tests for logging setup and RunContext audit writes."""

from __future__ import annotations

import pytest
from loguru import logger

from src.core.config import Config, LoggingConfig
from src.core.db import Database
from src.core.logging import setup_logging
from src.core.runlog import RunContext


def _file_sink_count(path) -> int:
    target = str(path)
    count = 0
    for handler in logger._core.handlers.values():
        sink = handler._sink
        candidate = getattr(sink, "_path", None) or str(sink)
        if target in str(candidate):
            count += 1
    return count


def test_runcontext_inserts_then_completes(tmp_path):
    db = Database(tmp_path / "signals.db")
    with RunContext(db, stage="collect") as ctx:
        assert len(ctx.run_id) == 8
        row = db.one("SELECT * FROM runs WHERE run_id = ?", (ctx.run_id,))
        assert row is not None
        assert row["stage"] == "collect"
        assert row["status"] == "running"
        assert row["started_at"]
        run_id = ctx.run_id
    row = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert row["status"] == "completed"
    assert row["finished_at"]
    db.close()


def test_runcontext_marks_failed_and_reraises(tmp_path):
    db = Database(tmp_path / "signals.db")
    with pytest.raises(RuntimeError, match="boom"):
        with RunContext(db, stage="collect") as ctx:
            run_id = ctx.run_id
            raise RuntimeError("boom")
    row = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert row["status"] == "failed"
    assert row["finished_at"]
    db.close()


def test_bump_accumulates(tmp_path):
    db = Database(tmp_path / "signals.db")
    with RunContext(db, stage="collect") as ctx:
        ctx.bump(accounts=2, documents=3)
        ctx.bump(accounts=1, signals_new=4, errors=1)
        run_id = ctx.run_id
    row = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert row["accounts"] == 3
    assert row["documents"] == 3
    assert row["signals_new"] == 4
    assert row["errors"] == 1
    db.close()


def test_log_fetch_writes_row_with_run_id(tmp_path):
    db = Database(tmp_path / "signals.db")
    with RunContext(db, stage="collect") as ctx:
        ctx.log_fetch(
            source="sec_edgar",
            url="https://data.sec.gov/x",
            status=200,
            elapsed_ms=12,
            bytes=100,
            domain="acme.com",
        )
        run_id = ctx.run_id
    row = db.one("SELECT * FROM fetch_log WHERE run_id = ?", (run_id,))
    assert row is not None
    assert row["source"] == "sec_edgar"
    assert row["url"] == "https://data.sec.gov/x"
    assert row["status"] == 200
    assert row["elapsed_ms"] == 12
    assert row["bytes"] == 100
    assert row["domain"] == "acme.com"
    assert row["cached"] == 0
    db.close()


def test_setup_logging_twice_one_file_sink(tmp_path):
    log_file = tmp_path / "signals.log"
    cfg = Config()
    cfg.logging = LoggingConfig(level="INFO", file=str(log_file), rotation="10 MB")
    setup_logging(cfg, run_id="abcd1234")
    setup_logging(cfg, run_id="abcd1234")
    assert _file_sink_count(log_file) == 1
    # reset loguru so later tests are not coupled
    logger.remove()
    logger.add(lambda _: None)

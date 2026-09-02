"""Tests for the run-scoped rotating log file sink (Plan Task 3)."""

from __future__ import annotations

import time

import pytest
from loguru import logger

from src.core.db import Database
from src.core import runlog
from src.core.runlog import RunContext, attach_file_sink


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    """Point RunContext's log dir at a tmp_path directory."""
    target = tmp_path / "logs"
    monkeypatch.setattr(runlog, "_resolve_logs_dir", lambda: str(target))
    return target


def _read(log_dir, run_id) -> str:
    path = log_dir / f"{run_id}.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def test_runcontext_writes_run_scoped_log_file(tmp_path, log_dir):
    db = Database(tmp_path / "signals.db")
    with RunContext(db, stage="collect") as ctx:
        logger.info("hello from run {}", ctx.run_id)
        run_id = ctx.run_id
        text = _read(log_dir, run_id)
        assert "hello from run" in text
    assert (log_dir / f"{run_id}.log").exists()
    assert f"{run_id}" in _read(log_dir, run_id)
    db.close()


def test_sink_removed_on_exit(tmp_path, log_dir):
    db = Database(tmp_path / "signals.db")
    with RunContext(db, stage="collect") as ctx:
        run_id = ctx.run_id
        logger.info("during run")
    logger.info("after exit marker")
    time.sleep(0.05)
    text = _read(log_dir, run_id)
    assert "during run" in text
    assert "after exit marker" not in text
    db.close()


def test_attach_file_sink_rotation_passthrough(tmp_path):
    log_dir = tmp_path / "logs2"
    sink_id = attach_file_sink("rottest1", log_dir, rotation="10 MB")
    try:
        logger.info("rotation passthrough line")
        time.sleep(0.05)
        path = log_dir / "rottest1.log"
        assert path.exists()
        assert "rotation passthrough line" in path.read_text(encoding="utf-8")
    finally:
        logger.remove(sink_id)


def test_attach_file_sink_creates_missing_dirs(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    sink_id = attach_file_sink("dirmake1", deep)
    try:
        assert deep.exists()
    finally:
        logger.remove(sink_id)

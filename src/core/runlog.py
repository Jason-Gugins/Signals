"""RunContext: run_id, counters, and writes to runs / fetch_log."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.core.db import Database

_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}"


def attach_file_sink(run_id: str, log_dir: str | Path, rotation: str = "10 MB") -> int:
    """Attach a rotating loguru file sink named for the run; return its sink id.

    Callers should ``logger.remove(sink_id)`` when the run ends.
    """
    path = Path(log_dir) / f"{run_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return logger.add(str(path), level="INFO", format=_LOG_FORMAT, rotation=rotation)


def _resolve_logs_dir() -> str:
    """Best-effort logs dir from config/default.yaml; falls back to data/logs."""
    try:
        import yaml  # local import: logging must never crash a run

        cfg_path = Path("config/default.yaml")
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            logs_dir = (data.get("logging") or {}).get("logs_dir")
            if logs_dir:
                return str(logs_dir)
    except Exception:
        pass
    return "data/logs"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RunContext:
    def __init__(self, db: Database, stage: str, run_id: str | None = None):
        self.db = db
        self.stage = stage
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.accounts = 0
        self.documents = 0
        self.signals_new = 0
        self.errors = 0
        self._sink_id: int | None = None

    def __enter__(self) -> "RunContext":
        self.db.execute(
            """
            INSERT INTO runs(run_id, stage, started_at, status,
                             accounts, documents, signals_new, errors)
            VALUES (?, ?, ?, 'running', 0, 0, 0, 0)
            """,
            (self.run_id, self.stage, _now()),
        )
        # Fail-safe: attach the run-scoped rotating file sink; logging must
        # never crash a run.
        try:
            self._sink_id = attach_file_sink(self.run_id, _resolve_logs_dir())
        except Exception:
            self._sink_id = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Fail-safe: detach the run's file sink before recording the outcome.
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except Exception:
                pass
            self._sink_id = None
        status = "failed" if exc_type is not None else "completed"
        self.db.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?,
                accounts = ?, documents = ?, signals_new = ?, errors = ?
            WHERE run_id = ?
            """,
            (
                _now(),
                status,
                self.accounts,
                self.documents,
                self.signals_new,
                self.errors,
                self.run_id,
            ),
        )

    def bump(self, *, accounts: int = 0, documents: int = 0, signals_new: int = 0, errors: int = 0) -> None:
        self.accounts += accounts
        self.documents += documents
        self.signals_new += signals_new
        self.errors += errors
        self.db.execute(
            """
            UPDATE runs
            SET accounts = ?, documents = ?, signals_new = ?, errors = ?
            WHERE run_id = ?
            """,
            (self.accounts, self.documents, self.signals_new, self.errors, self.run_id),
        )

    def log_fetch(
        self,
        *,
        source: str,
        url: str,
        status: int,
        elapsed_ms: int,
        bytes: int = 0,
        cached: bool = False,
        domain: str | None = None,
        error: str | None = None,
        error_class: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO fetch_log(run_id, source, domain, url, status,
                                  elapsed_ms, bytes, cached, error, error_class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                source,
                domain,
                url,
                status,
                elapsed_ms,
                bytes,
                1 if cached else 0,
                error,
                error_class,
            ),
        )

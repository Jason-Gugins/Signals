"""RunContext: run_id, counters, and writes to runs / fetch_log."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.core.db import Database


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

    def __enter__(self) -> "RunContext":
        self.db.execute(
            """
            INSERT INTO runs(run_id, stage, started_at, status,
                             accounts, documents, signals_new, errors)
            VALUES (?, ?, ?, 'running', 0, 0, 0, 0)
            """,
            (self.run_id, self.stage, _now()),
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
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
    ) -> None:
        self.db.execute(
            """
            INSERT INTO fetch_log(run_id, source, domain, url, status,
                                  elapsed_ms, bytes, cached, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

"""Play hit-rate backtesting (P2 Task 16) — local, no CRM.

Records per-play outcomes locally (play_outcomes) and computes per-play
conversion rates. These rates feed the existing `calibration` table
(per source + signal_type samples/hits) once enough samples accumulate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.db import Database

MIN_SAMPLES = 30


def record_outcome(db: "Database", domain: str, play_id: str, outcome: str) -> None:
    """Record a play outcome. PK (domain, play_id): re-recording upserts."""
    db.execute(
        """
        INSERT INTO play_outcomes (domain, play_id, outcome, decided_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(domain, play_id)
        DO UPDATE SET outcome = excluded.outcome, decided_at = excluded.decided_at
        """,
        (domain, play_id, outcome, datetime.now(timezone.utc).isoformat()),
    )


def play_hit_rates(db: "Database") -> dict[str, dict]:
    """Per-play hit rates: {play_id: {sent, hit, rate}}.

    sent = number of play_assignments rows for the play;
    hit  = number of play_outcomes rows with outcome == 'hit';
    rate = hit / sent (0.0 when sent == 0).
    """
    sent_rows = db.query(
        """
        SELECT play_id, COUNT(*) AS sent
        FROM play_assignments
        GROUP BY play_id
        """
    )
    hit_rows = db.query(
        """
        SELECT pa.play_id AS play_id, COUNT(*) AS hit
        FROM play_outcomes po
        JOIN play_assignments pa
          ON pa.domain = po.domain AND pa.play_id = po.play_id
        WHERE po.outcome = 'hit'
        GROUP BY pa.play_id
        """
    )
    sent = {r["play_id"]: r["sent"] for r in sent_rows}
    hits = {r["play_id"]: r["hit"] for r in hit_rows}
    rates: dict[str, dict] = {}
    for play_id, n_sent in sent.items():
        n_hit = hits.get(play_id, 0)
        rates[play_id] = {
            "sent": n_sent,
            "hit": n_hit,
            "rate": (n_hit / n_sent) if n_sent else 0.0,
        }
    return rates


def feed_calibration(db: "Database", *, min_samples: int = MIN_SAMPLES) -> None:
    """Write (source, signal_type) samples/hits into the calibration table.

    For each (source, signal_type) pair derivable by joining
    play_assignments.signal_id -> signals, aggregate decided outcomes.
    Only writes when samples >= min_samples (below that: no-op —
    `blend_confidence` won't use the row anyway). Upserts on the
    (source, signal_type) PK, so re-running the feed refreshes counts.
    """
    rows = db.query(
        """
        SELECT s.source   AS source,
               s.signal_type AS signal_type,
               COUNT(*)   AS samples,
               SUM(CASE WHEN po.outcome = 'hit' THEN 1 ELSE 0 END) AS hits
        FROM play_assignments pa
        JOIN signals s         ON s.signal_id = pa.signal_id
        LEFT JOIN play_outcomes po
               ON po.domain = pa.domain AND po.play_id = pa.play_id
        GROUP BY s.source, s.signal_type
        HAVING COUNT(*) >= ?
        """,
        (min_samples,),
    )
    for r in rows:
        db.execute(
            """
            INSERT INTO calibration (source, signal_type, samples, hits)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, signal_type)
            DO UPDATE SET samples = excluded.samples, hits = excluded.hits
            """,
            (r["source"], r["signal_type"], r["samples"], r["hits"] or 0),
        )

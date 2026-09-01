"""Per-source health report aggregated from fetch_log (Task 22).

SQL-side aggregation only — one grouped query produces one row per source
within the window; no per-row Python loops.
"""

from __future__ import annotations


def source_health(db, *, since_hours: int = 168) -> list[dict]:
    """One health row per source seen in ``fetch_log`` within ``since_hours``.

    Returns ``[]`` for an empty log. Each row:
      source, fetched, cached, failed, error_class (histogram dict),
      last_success, success_rate (2 decimals).
    """
    rows = db.query(
        """
        SELECT source,
               COUNT(*) AS fetched,
               SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cached,
               SUM(CASE WHEN (status IS NOT NULL AND status >= 400) OR error IS NOT NULL
                        THEN 1 ELSE 0 END) AS failed,
               MAX(CASE WHEN (status IS NULL OR status < 400) AND error IS NULL
                        THEN at END) AS last_success
        FROM fetch_log
        WHERE at >= datetime('now', ?)
        GROUP BY source
        ORDER BY source
        """,
        (f"-{int(since_hours)} hours",),
    )

    hist: dict[str, dict[str, int]] = {}
    for h in db.query(
        """
        SELECT source, error_class, COUNT(*) AS n
        FROM fetch_log
        WHERE at >= datetime('now', ?) AND error_class IS NOT NULL
        GROUP BY source, error_class
        """,
        (f"-{int(since_hours)} hours",),
    ):
        hist.setdefault(h["source"], {})[h["error_class"]] = h["n"]

    out = []
    for r in rows:
        fetched = r["fetched"] or 0
        failed = r["failed"] or 0
        out.append(
            {
                "source": r["source"],
                "fetched": fetched,
                "cached": r["cached"] or 0,
                "failed": failed,
                "error_class": hist.get(r["source"], {}),
                "last_success": r["last_success"],
                "success_rate": round(1 - failed / fetched, 2) if fetched else 0.0,
            }
        )
    return out

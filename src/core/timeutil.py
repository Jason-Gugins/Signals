"""The age-reference clock: one UTC source of truth for signal ages.

Stdlib-only and I/O-free, so every layer that needs the age reference — the
signals consumers, the pipeline producers, the CLI/export paths — can import it
without pulling in the pipeline or a database handle.

Signal ages are ``(reference - observed_at).days``, and every ``observed_at``
is an aware-UTC ISO string. The reference must therefore be the UTC date; a
local-clock reference makes fresh signals age-negative whenever local time
lags UTC past midnight.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utc_today(now: datetime | None = None) -> date:
    """The age reference date, in UTC - the single source of truth for ages.

    observed_at values are aware-UTC ISO strings, so the reference must be the UTC
    date. Using date.today() (local) made ages negative for fresh signals whenever
    local time lagged UTC past midnight (reproduced 2026-09-15: local 2026-09-14
    against observed_at 2026-09-15T00:30Z -> age -1).
    """
    return (now or datetime.now(timezone.utc)).date()

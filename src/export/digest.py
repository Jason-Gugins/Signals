"""Per-account alert digests: daily/weekly markdown with why-now lines.

Pure rendering — no DB, no file I/O, no wall-clock time.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable, Optional

from src.core.models import Signal
from src.signals.plays import PlayAssignment
from src.signals.taxonomy import Taxonomy


def _fmt_amount(amount_usd) -> Optional[str]:
    try:
        n = float(amount_usd)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"${m:.0f}M" if m == int(m) else f"${m:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


def _why_now(signal: Signal) -> Optional[str]:
    """Derive a why-now line from evidence_data, with graceful fallback."""
    d = signal.evidence_data or {}
    if not isinstance(d, dict):
        d = {}
    parts: list[str] = []
    if d.get("amount_usd") is not None:
        amt = _fmt_amount(d["amount_usd"])
        stage = d.get("stage")
        if stage:
            parts.append(f"raised {amt} at {stage}")
        elif amt:
            parts.append(f"raised {amt}")
    elif d.get("stage"):
        parts.append(f"at {d['stage']} stage")
    if d.get("role_bucket"):
        parts.append(f"hiring for {d['role_bucket']} roles")
    for key, verb in (
        ("headcount", "headcount now"),
        ("title", "new title"),
        ("company", "company update"),
        ("note", "note"),
    ):
        if d.get(key) and key not in ("amount_usd", "stage", "role_bucket"):
            parts.append(f"{verb}: {d[key]}")
    if parts:
        return "Why now: " + "; ".join(parts) + "."
    if signal.evidence:
        ev = signal.evidence if len(signal.evidence) <= 200 else signal.evidence[:197] + "..."
        return f"Why now: {ev}"
    return None


def _date_range(signals: Iterable[Signal]) -> str:
    dates = sorted({s.observed_at for s in signals if s.observed_at})
    if not dates:
        return "no dated signals"
    lo, hi = dates[0], dates[-1]
    return f"{lo} to {hi}" if hi > lo else lo


def build_digest(
    domain: str,
    signals: list[Signal],
    plays: list[PlayAssignment],
    *,
    period: str,
    taxonomy: Optional[Taxonomy] = None,
    since: Optional[str] = None,
) -> str:
    """Render a markdown alert digest for one account.

    ``since`` (inclusive ISO date or datetime string) bounds the window:
    signals observed before it are excluded, so a daily digest is actually
    about the last day rather than full history. ``None`` keeps every
    signal (used by tests and callers that pre-filter).
    """
    tax = taxonomy or Taxonomy.load()
    if since:
        signals = [s for s in signals if (s.observed_at or "") >= since]
    lines: list[str] = [f"# {domain} — {period} digest ({_date_range(signals)})"]
    if not signals:
        lines.append("")
        lines.append("No signals this period.")
    else:
        groups: "OrderedDict[str, list[Signal]]" = OrderedDict()
        for s in signals:
            groups.setdefault(s.signal_type, []).append(s)
        for stype, members in groups.items():
            try:
                label = tax.get(stype).label
            except Exception:
                label = stype
            lines.append("")
            lines.append(f"## {label} ({len(members)})")
            for s in members:
                head = s.title or s.evidence or s.signal_id
                lines.append(f"- {s.observed_at} — {head}")
                why = _why_now(s)
                if why:
                    lines.append(f"  - {why}")
    lines.append("")
    lines.append("## Top plays")
    top = list(plays)[:2]
    if not top:
        lines.append("")
        lines.append("No plays assigned this period.")
    for i, p in enumerate(top, 1):
        lines.append("")
        lines.append(f"### {i}. {p.play_id} ({p.urgency})")
        lines.append("")
        lines.append(p.opener)
        lines.append("")
        lines.append(p.t24)
    lines.append("")
    return "\n".join(lines)

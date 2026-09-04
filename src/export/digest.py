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


def _is_stub_domain(domain: str) -> bool:
    """Form D stub-account domain: ``cikXXXXXXXXXX.edgar``.

    Mirrors ``formd_identity._is_stub`` without importing the private helper
    (digest rendering must stay independent of the SEC source layer).
    """
    return bool(domain) and domain.startswith("cik") and domain.endswith(".edgar")


FORMD_SECTION_TITLE = "New Form D issuers (unmatched)"
FORMD_SECTION_CAP = 25


def _formd_unmatched_rows(signals: Iterable[Signal]) -> list[str]:
    """One line per funding_form_d signal, sorted by amount desc, capped."""
    rows: list[tuple[float, str]] = []
    for s in signals:
        if s.signal_type != "funding_form_d":
            continue
        d = s.evidence_data if isinstance(s.evidence_data, dict) else {}
        entity = d.get("entity_name") or s.title or s.domain
        amt = d.get("amount_usd")
        try:
            amt_f = float(amt)
        except (TypeError, ValueError):
            amt_f = 0.0
        amt_s = _fmt_amount(amt_f) if amt is not None else (d.get("amount_display") or "?")
        state = d.get("state") or "—"
        cik = d.get("cik") or ""
        filed = s.observed_at or "?"
        rows.append((amt_f, f"- {entity} — {amt_s} — {state} — filed {filed} — CIK {cik}"))
    rows.sort(key=lambda t: t[0], reverse=True)
    return [line for _, line in rows[:FORMD_SECTION_CAP]]


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
    include_formd_unmatched: bool = False,
) -> str:
    """Render a markdown alert digest for one account.

    ``since`` (inclusive ISO date or datetime string) bounds the window:
    signals observed before it are excluded, so a daily digest is actually
    about the last day rather than full history. ``None`` keeps every
    signal (used by tests and callers that pre-filter).

    ``include_formd_unmatched`` appends the "New Form D issuers (unmatched)"
    section for stub-account (cik*.edgar) funding_form_d signals in the
    window. Only the global/default render enables it — per-domain renders
    pass False (default), so stubs never leak into a named account's digest.
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
        if include_formd_unmatched:
            stub_sigs = [s for s in signals if _is_stub_domain(s.domain or "")]
            rows = _formd_unmatched_rows(stub_sigs)
            if rows:
                lines.append("")
                lines.append(f"## {FORMD_SECTION_TITLE}")
                lines.extend(rows)
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

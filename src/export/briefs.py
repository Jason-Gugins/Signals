"""Per-account Markdown brief generator."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.models import Account, Contact, Signal
from src.signals.contacts import best_contact
from src.signals.evidence import render_evidence
from src.signals.plays import PlayAssignment
from src.signals.score import ScoreResult
from src.signals.tier import TierResult


def _hq(account: Account) -> str:
    parts = [p for p in (account.hq_city, account.hq_region, account.hq_country) if p]
    return ", ".join(parts) or "—"


def render_brief(
    account: Account,
    signals: list[Signal],
    score: ScoreResult,
    tier: TierResult,
    plays: list[PlayAssignment],
    contacts: list[Contact],
    *,
    today: date,
) -> str:
    company = account.name or account.domain
    lines = [
        f"# {company}  ·  Tier {tier.tier}  ·  Score {score.score}  ·  Window: {tier.buying_window}",
        f"{account.domain} · {account.industry or '—'} · {account.employee_count or '—'} employees · {_hq(account)}",
        f"_Rationale: {tier.rationale}_",
        "",
        "## Why now (top signals)",
        "| When | Signal | Evidence | Source | Conf |",
        "|------|--------|----------|--------|------|",
    ]
    by_id = {s.signal_id: s for s in signals}
    valued = [c for c in score.contributions if not c.dropped_reason]
    valued.sort(key=lambda c: c.value, reverse=True)
    for c in valued[:8]:
        sig = by_id.get(c.signal_id)
        when = sig.observed_at if sig else ""
        ev = (sig.evidence if sig and sig.evidence else "") or (
            render_evidence(sig, account=account, today=today) if sig else ""
        )
        ev = ev.replace("|", "/")
        conf = f"{c.confidence:.2f}"
        lines.append(f"| {when} | {c.signal_type} | {ev} | {c.source} | {conf} |")
    if not valued:
        lines.append("| — | — | No contributing signals | — | — |")

    lines += ["", "## Stacked plays"]
    if not plays:
        lines.append("_No play assigned._")
    for play in plays:
        trigger = play.signal_id or "combo"
        lines.append(f"### {play.rank}. {play.play_name}  ({trigger}, urgency {play.urgency})")
        lines.append(f"**Opener:** {play.opener}")
        lines.append(f"**T24 profiling message:** {play.t24}")
        lines.append(f"**CTA:** {play.cta}")
        lines.append(f"**If they push back:** {play.loss_aversion}")
        lines.append("")

    lines += ["## Who to contact", "| Name | Title | Persona | Why them | LinkedIn |", "|------|-------|---------|----------|----------|"]
    if not contacts:
        lines.append(f"| — | — | — | No contact identified — run `signals deepen --domain {account.domain}` | — |")
    else:
        for play in plays or [None]:
            pick = best_contact(contacts, play.play_id if play else "growth_pitch", None)
            if not pick:
                continue
            why = play.play_name if play else "default"
            lines.append(
                f"| {pick.name or ''} | {pick.title or ''} | {pick.persona or ''} | {why} | {pick.linkedin_url or ''} |"
            )

    lines += ["", "## Evidence trail"]
    trail = sorted(signals, key=lambda s: s.observed_at, reverse=True)
    for sig in trail:
        url = sig.url or ""
        lines.append(f"- {sig.observed_at} · {sig.signal_type} · {url}")
    if not trail:
        lines.append("- none")

    lines += ["", "## Watch items"]
    watches = []
    for sig in signals:
        data = sig.evidence_data or {}
        if data.get("renewal_estimate"):
            watches.append(f"Renewal estimate {data['renewal_estimate']} ({data.get('confidence_note', 'estimated')})")
    if score.score and score.score < 20:
        watches.append("Score is below active threshold — nurture / watchlist.")
    if not watches:
        watches.append("No dated watch items.")
    for w in watches:
        lines.append(f"- {w}")

    sources = sorted({s.source for s in signals})
    lines += [
        "",
        f"_Generated {today.isoformat()} from {len(signals)} signals across {len(sources)} sources. Estimates are marked as such._",
        "",
    ]
    return "\n".join(lines)


def write_brief(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return str(p)

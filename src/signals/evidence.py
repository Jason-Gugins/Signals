"""One-line human evidence a rep can paste into an email."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from src.core.models import Account, Signal
from src.core.textutil import to_iso_date, truncate
from src.signals.taxonomy import Taxonomy, UnknownSignalType


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


TEMPLATES: dict[str, str] = {
    "funding_round": "Raised {amount_display} {round_stage} ({observed_human})",
    "funding_form_d": "Form D filed {amount_display} ({observed_human})",
    "champion_migration": "{person_name} moved from {prior_company} to {company} as {new_role} ({observed_human})",
    "hiring_surge": "{job_count} new {department} roles posted in the last {window_days} days",
    "renewal_window": "{competitor} first detected {first_seen_human} — renewal window ~{renewal_month}",
    "layoff": "WARN notice: {affected} roles at {location} effective {effective_date}",
    "intent_1st_owned": "{visits} visits to {page_label} ({observed_human})",
    "tech_migration_mentioned": "Job post mentions migrating from {from_tech} to {to_tech}",
    "exec_hire": "{person_name} joined as {new_role} ({observed_human})",
    "product_launch": "Launched {product} ({observed_human})",
}

_SPACES = re.compile(r" {2,}")
_TAX: Taxonomy | None = None


def _taxonomy() -> Taxonomy:
    global _TAX
    if _TAX is None:
        _TAX = Taxonomy.load()
    return _TAX


def humanize_age(observed_at: str, today: date) -> str:
    iso = to_iso_date(observed_at)
    if not iso:
        return ""
    observed = date.fromisoformat(iso)
    days = (today - observed).days
    if days < 0:
        return f"on {iso}"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 400:
        months = max(1, days // 30)
        unit = "month" if months == 1 else "months"
        return f"{months} {unit} ago"
    return f"on {iso}"


def render_evidence(
    signal: Signal,
    *,
    account: Account | None = None,
    today: date | None = None,
) -> str:
    today = today or date.today()
    observed_human = ""
    iso = to_iso_date(signal.observed_at)
    if iso:
        observed_human = humanize_age(iso, today)
    vars_ = SafeDict(signal.evidence_data or {})
    vars_["observed_human"] = observed_human
    vars_["observed_at"] = iso or ""
    vars_["title"] = signal.title or ""
    vars_["company"] = (account.name if account and account.name else "") or signal.domain
    tmpl = TEMPLATES.get(signal.signal_type)
    if tmpl:
        text = tmpl.format_map(vars_)
    else:
        try:
            label = _taxonomy().get(signal.signal_type).label
        except UnknownSignalType:
            label = signal.signal_type.replace("_", " ")
        title = signal.title or ""
        text = f"{label} — {title}".strip(" —")
    text = _SPACES.sub(" ", text).strip()
    text = text.replace(" .", ".").rstrip()
    if text.endswith(".."):
        text = text[:-1]
    text = truncate(text, 200) or ""
    return text

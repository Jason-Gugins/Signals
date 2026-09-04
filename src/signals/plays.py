"""Play mapping and SafeDict template rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.core.config import Config
from src.core.models import Account, Contact, Signal
from src.signals.evidence import SafeDict
from src.signals.health import health_score
from src.signals.score import ScoreResult
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult


COMBO_PLAY = {
    "super_signal": "t24_profiling",
    "champion_play": "relationship_pitch",
    "deep_pockets": "growth_pitch",
    "marketplace": "marketplace_compare",
    "efficiency_pivot": "automation_pitch",
    "displacement_clock": "displacement_pitch",
    "contextual_cold": "contextual_cold",
    "turnaround_pitch": "turnaround_pitch",
    "relocation_window": "infrastructure_pitch",
}


def validate_combo_coverage() -> list[str]:
    """Return every emittable combo id that has no COMBO_PLAY entry.

    The universe of emittable combo ids is config-driven: the ``combos`` list
    in ``config/scoring.yaml`` (consumed by
    :func:`src.signals.combos.evaluate_combos` and read via
    ``Config.load_yaml("scoring")``). A combo id missing from ``COMBO_PLAY``
    silently yields ``play_id=""`` in :func:`assign_plays`, so we surface the
    gaps instead of letting them slip through.
    """
    try:
        scoring = Config.load().load_yaml("scoring")
    except Exception:
        return []
    combo_defs = scoring.get("combos") or []
    universe = {str(spec.get("id")) for spec in combo_defs if spec.get("id")}
    return sorted(universe - set(COMBO_PLAY))

_SPACES = re.compile(r" {2,}")


@dataclass
class PlayAssignment:
    play_id: str
    play_name: str
    signal_id: str | None
    rank: int
    urgency: int
    variables: dict
    opener: str
    t24: str
    cta: str
    loss_aversion: str


def render(template: str, variables: dict) -> str:
    text = (template or "").format_map(SafeDict(variables))
    return _SPACES.sub(" ", text).strip()


def build_variables(
    account: Account,
    signal: Signal | None,
    contact: Contact | None,
    *,
    extra: dict | None = None,
) -> dict:
    first = last = title = dept = ""
    if contact:
        parts = (contact.name or "").split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        title = contact.title or ""
        dept = contact.department or ""
    vars_: dict = {
        "company": account.name or account.domain,
        "domain": account.domain,
        "industry": account.industry or "",
        "employee_count": account.employee_count or "",
        "first_name": first,
        "last_name": last,
        "title": title,
        "department": dept,
    }
    if signal and signal.evidence_data:
        vars_.update({k: v for k, v in signal.evidence_data.items() if v is not None})
    if extra:
        vars_.update(extra)
    return vars_


def _play_table(plays_cfg: dict) -> dict:
    return plays_cfg.get("plays", plays_cfg)


def _render_play(play_id: str, table: dict, variables: dict, *, signal_id, rank, urgency) -> PlayAssignment | None:
    spec = table.get(play_id)
    if not spec:
        return None
    required = spec.get("required_vars") or []
    missing = [v for v in required if not variables.get(v)]
    opener_tmpl = spec.get("opener_fallback") if missing else spec.get("opener")
    t24_tmpl = spec.get("t24_fallback") if missing else spec.get("t24")
    if missing:
        opener_tmpl = opener_tmpl or spec.get("opener") or "Wanted to compare notes on {company}."
        t24_tmpl = t24_tmpl or spec.get("t24") or "Hi {first_name}, quick question about {company}."
    opener = render(opener_tmpl, variables)
    t24 = render(t24_tmpl, variables)
    if not opener or not t24:
        opener = opener or render("Quick note on {company}.", variables)
        t24 = t24 or render("Hi {first_name}, circling back on {company}.", variables)
    return PlayAssignment(
        play_id=play_id,
        play_name=spec.get("name") or play_id,
        signal_id=signal_id,
        rank=rank,
        urgency=urgency,
        variables=variables,
        opener=opener,
        t24=t24,
        cta=render(spec.get("cta") or "", variables),
        loss_aversion=render(spec.get("loss_aversion") or "", variables),
    )


def assign_plays(
    account,
    signals,
    score: ScoreResult,
    tier: TierResult,
    *,
    taxonomy: Taxonomy,
    plays_cfg: dict,
    contacts: list[Contact],
    today,
    max_plays: int = 3,
) -> list[PlayAssignment]:
    table = _play_table(plays_cfg)
    by_id = {s.signal_id: s for s in signals}
    chosen: list[PlayAssignment] = []
    seen: set[str] = set()

    # Health gate: when the account's aggregate signal polarity (health score,
    # -1.0..1.0 from src/signals/health.py) falls below the threshold, plays in
    # the suppress list are skipped — e.g. don't pitch growth to a company that
    # just announced layoffs. Threshold and suppressed families are
    # config-overridable via plays_cfg["health_gate"]
    # (config/plays.yaml health_gate: {threshold: <float>, suppress_plays:
    # [<play_id>...]}). Defaults: threshold -0.5, suppress growth_pitch.
    # Per-type weights come from config/health.yaml (top-level "weights" map);
    # missing file/section → health_score's built-in defaults.
    gate_cfg = plays_cfg.get("health_gate") or {}
    threshold = float(gate_cfg.get("threshold", -0.5))
    suppressed = set(gate_cfg.get("suppress_plays") or ["growth_pitch"])
    try:
        weights = (Config.load().load_yaml("health") or {}).get("weights") or None
    except Exception:
        weights = None
    health, _reasons = health_score(signals, weights=weights)
    gated = health < threshold

    def add(play_id: str, signal: Signal | None, urgency: int) -> None:
        if play_id in seen or len(chosen) >= max_plays:
            return
        if gated and play_id in suppressed:
            return
        contact = contacts[0] if contacts else None
        variables = build_variables(account, signal, contact)
        item = _render_play(play_id, table, variables, signal_id=signal.signal_id if signal else None, rank=len(chosen) + 1, urgency=urgency)
        if item and item.opener and item.t24:
            seen.add(play_id)
            chosen.append(item)

    for combo in score.combos:
        play_id = COMBO_PLAY.get(combo.get("id"), "")
        sid = (combo.get("matched_signal_ids") or [None])[0]
        add(play_id, by_id.get(sid) if sid else None, int(combo.get("urgency") or 0))

    valued = [c for c in score.contributions if not c.dropped_reason]
    valued.sort(key=lambda c: c.value, reverse=True)
    for c in valued:
        try:
            play_id = taxonomy.get(c.signal_type).play
        except Exception:
            continue
        add(play_id, by_id.get(c.signal_id), 0)

    if tier.tier == 4 and not chosen:
        add("flattery_opener", signals[0] if signals else None, 1)

    return chosen[:max_plays]

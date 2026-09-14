"""Authoritative per-account intelligence snapshot (frozen, computed once).

This module is the SINGLE authoritative calculation that the dossier
consumes: for one account and one ``today`` it partitions signals into
active/expired, evaluates combos, scores the account, assigns a tier,
maps plays, selects persona framing, and evaluates ICP fit. Every field on
the returned :class:`IntelligenceSnapshot` is produced from that one pass,
so no downstream consumer (dossier, export, report) needs to re-query
``play_assignments`` or recompute combos and risk disagreeing with the
persisted score.

Expired signals (past their type's ``supersede_days`` window) NEVER
contribute to score, tier, combos, or plays — they are only retained on the
snapshot as :attr:`IntelligenceSnapshot.expired_signals` for transparency.

``persona_framing`` is nullable framing TEXT (a full sentence such as
"Frame this as a revenue conversation: ..."), NOT a bucket name. Do not
compare it against ``"revenue"``/``"tech"``/``"exec"``; it is ``None`` when
no contact with a recognizable function is known.

Supersede map choice: this function accepts the PRE-BUILT
``supersede_days_by_type`` mapping (produced by
:func:`src.signals.lifecycle.load_supersede_map` on the parsed
``config/signals.yaml``). Callers that already load that map (the
orchestrator does, once per ``score()`` run) pass it in rather than having
this module re-parse the YAML. ``None``/empty means no type expires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.models import Account, Contact, Signal
from src.export.briefs import select_persona_framing
from src.identity.icp import IcpResult, evaluate_icp
from src.signals.combos import evaluate_combos
from src.signals.lifecycle import partition_signals
from src.signals.plays import PlayAssignment, assign_plays
from src.signals.score import ScoreResult, score_account
from src.signals.taxonomy import Taxonomy
from src.signals.tier import TierResult, assign_tier


@dataclass(frozen=True)
class IntelligenceSnapshot:
    """One account's intelligence, computed once for one ``today``.

    Expanded/tuple containers are immutable so a consumer cannot mutate the
    authoritative calculation after the fact.
    """

    domain: str
    account: Account
    active_signals: tuple[Signal, ...]
    expired_signals: tuple[Signal, ...]
    score: ScoreResult
    tier: TierResult
    combos: tuple[dict, ...]
    plays: tuple[PlayAssignment, ...]
    contacts: tuple[Contact, ...]
    persona_framing: str | None
    fit: IcpResult | None
    today: date


def build_intelligence_snapshot(
    *,
    account: Account,
    signals,
    taxonomy: Taxonomy,
    scoring_cfg: dict,
    plays_cfg: dict,
    icp_rules,
    contacts,
    today: date,
    supersede_days_by_type: dict[str, int] | None = None,
    calibration_stats: dict | None = None,
) -> IntelligenceSnapshot:
    """Compute the one authoritative snapshot for ``account`` as of ``today``.

    Ordering is significant and each step runs exactly once:
    partition -> combos -> score -> tier -> plays -> persona framing -> ICP fit.
    """
    active, expired = partition_signals(
        signals, today=today, supersede_days_by_type=supersede_days_by_type or {}
    )
    contact_list = list(contacts or [])

    combos = evaluate_combos(active, scoring_cfg.get("combos") or [], today=today)
    score = score_account(
        account,
        active,
        taxonomy=taxonomy,
        cfg=scoring_cfg,
        today=today,
        combos=combos,
        calibration_stats=calibration_stats,
    )
    tier = assign_tier(active, score, taxonomy=taxonomy, cfg=scoring_cfg, today=today)
    plays = assign_plays(
        account,
        active,
        score,
        tier,
        taxonomy=taxonomy,
        plays_cfg=plays_cfg,
        contacts=contact_list,
        today=today,
    )
    persona_framing = select_persona_framing(contact_list, plays)
    fit = evaluate_icp(account, icp_rules, signals=active) if icp_rules else None

    return IntelligenceSnapshot(
        domain=account.domain,
        account=account,
        active_signals=tuple(active),
        expired_signals=tuple(expired),
        score=score,
        tier=tier,
        combos=tuple(combos),
        plays=tuple(plays),
        contacts=tuple(contact_list),
        persona_framing=persona_framing,
        fit=fit,
        today=today,
    )
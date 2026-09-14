"""Portable, evidence-linked intel package (Task 11).

One dossier per account, self-contained: account identity, fit, active and
expired signals, the promoted operational needs and required-stack demands, the
plays, the combos, the people (non-contact-detail fields only), coverage rows
and gaps -- plus an ordered evidence index.

Portability
    The written package is readable with no database and no repo object in
    scope (plain JSON + Markdown + JSONL).

Auditability
    Every claim in the dossier references one or more ``evidence_id`` values,
    and each id resolves to exactly one record in ``evidence.jsonl``.

Non-leakage
    Evidence ``detail`` strings are bounded to :data:`EVIDENCE_TEXT_LIMIT`
    characters and are drawn only from the signal's own rendered evidence line
    or its verbatim quote -- never a whole document body, contact detail,
    cookie/session data, or credential-looking text.

:func:`build_dossier` performs no I/O and reads no wall clock (it uses the
snapshot's ``today`` and the ``generated_at`` it is handed); the only
filesystem writer is :func:`write_intel_package`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.textutil import to_iso_date
from src.intel.coverage import coverage_summary
from src.signals.evidence import render_evidence
from src.signals.taxonomy import Taxonomy, UnknownSignalType

KIND = "intel_package"
SCHEMA_VERSION = 1
EVIDENCE_TEXT_LIMIT = 400

#: At most this many non-contact-detail people entries ride in the dossier.
MAX_PEOPLE = 5

#: At most this many highest-confidence signals render into the markdown.
MAX_MARKDOWN_SIGNALS = 10

_NEED = "need_statement"
_DEMAND = "required_stack_demand"

_IDENTITY_FIELDS = (
    "cik",
    "ticker",
    "ats_vendor",
    "ats_token",
    "careers_url",
    "linkedin_slug",
    "g2_slug",
    "app_store_id",
    "blog_feed_url",
    "industry",
    "employee_count",
    "hq_city",
    "hq_country",
    "tier",
    "score",
    "buying_window",
)

_TAXONOMY: Taxonomy | None = None


def _taxonomy() -> Taxonomy:
    global _TAXONOMY
    if _TAXONOMY is None:
        _TAXONOMY = Taxonomy.load()
    return _TAXONOMY


def _bound(text) -> str:
    """Bounded evidence text -- never a whole document body."""
    if text is None:
        return ""
    return str(text)[:EVIDENCE_TEXT_LIMIT]


def _age_days(observed_at, today: date):
    iso = to_iso_date(observed_at)
    if not iso:
        return None
    try:
        return (today - date.fromisoformat(iso[:10])).days
    except ValueError:
        return None


def _spec(signal_type: str):
    try:
        return _taxonomy().get(signal_type)
    except UnknownSignalType:
        return None


def _signal_evidence_text(sig, *, account, today: date) -> str:
    """A signal's evidence line, never blank.

    Prefers the persisted ``evidence`` field; falls back to
    :func:`render_evidence` (the same fallback the briefs use), then the title,
    then the taxonomy label.
    """
    text = (sig.evidence or "").strip()
    if not text:
        text = (render_evidence(sig, account=account, today=today) or "").strip()
    if not text:
        text = (sig.title or "").strip()
    if not text:
        spec = _spec(sig.signal_type)
        text = spec.label if spec is not None else sig.signal_type.replace("_", " ")
    return text or "(no evidence text)"


def _fmt(value) -> str:
    """Render a dossier value for markdown without inventing one."""
    if value is None or value == "":
        return "—"
    return str(value)


def build_dossier(
    snapshot,
    *,
    coverage,
    market_profile=None,
    market_profile_id=None,
    jobs_summary=None,
    gaps=None,
    max_signals=None,
    generated_at=None,
    invocation_id=None,
) -> dict:
    """Build the JSON-serialisable dossier for ``snapshot``.

    ``coverage`` is the coverage row list (or a ``{"rows", "summary"}`` dict).
    ``max_signals`` caps how many ACTIVE signals appear in the signals list
    (default: all); the highest-confidence signals are kept, the needs and
    demands lists stay complete, and the number omitted is recorded in
    ``signals.counts.active_omitted`` so the cap is never silent.
    """
    today = snapshot.today
    account = snapshot.account
    domain = snapshot.domain

    evidence: list[dict] = []
    evidence_by_signal: dict[str, str] = {}

    def add_evidence(
        *,
        kind: str,
        signal_id: str,
        detail,
        url=None,
        source=None,
        observed_at=None,
        doc_id=None,
        job_key=None,
    ) -> str:
        """Append one bounded evidence record; ids are append-order deterministic."""
        evidence_id = f"ev-{len(evidence) + 1:04d}"
        record = {
            "evidence_id": evidence_id,
            "kind": kind,
            "signal_id": signal_id,
            "detail": _bound(detail),
            "url": url,
            "source": source,
            "observed_at": observed_at,
        }
        if doc_id is not None:
            record["doc_id"] = doc_id
        if job_key is not None:
            record["job_key"] = job_key
        evidence.append(record)
        return evidence_id

    def signal_entry(sig) -> dict:
        entry = {
            "signal_id": sig.signal_id,
            "signal_type": sig.signal_type,
            "category": sig.category,
            "polarity": sig.polarity,
            "degree": sig.degree,
            "title": sig.title,
            "summary": sig.summary,
            "evidence": _signal_evidence_text(sig, account=account, today=today),
            "url": sig.url,
            "source": sig.source,
            "confidence": sig.confidence,
            "observed_at": sig.observed_at,
            "age_days": _age_days(sig.observed_at, today),
            "evidence_ids": [evidence_by_signal[sig.signal_id]],
        }
        spec = _spec(sig.signal_type)
        if spec is not None:
            entry["degree"] = spec.degree
            entry["half_life_days"] = spec.half_life_days
        return entry

    active_all = list(snapshot.active_signals)
    expired_all = list(snapshot.expired_signals)

    # Deterministic evidence order: active signals, then expired signals, then
    # the promoted needs and demands (which are subsets of the active list).
    for sig in active_all + expired_all:
        evidence_by_signal[sig.signal_id] = add_evidence(
            kind="signal",
            signal_id=sig.signal_id,
            detail=_signal_evidence_text(sig, account=account, today=today),
            url=sig.url,
            source=sig.source,
            observed_at=sig.observed_at,
        )

    needs: list[dict] = []
    demands: list[dict] = []
    for sig in active_all:
        data = sig.evidence_data or {}
        if sig.signal_type == _NEED:
            quote = data.get("quote")
            evidence_id = add_evidence(
                kind="need",
                signal_id=sig.signal_id,
                detail=quote,
                url=data.get("url"),
                source=data.get("source"),
                observed_at=sig.observed_at,
                doc_id=data.get("doc_id"),
            )
            needs.append(
                {
                    "signal_id": sig.signal_id,
                    "quote": quote,
                    "matched_phrase": data.get("matched_phrase"),
                    "doc_id": data.get("doc_id"),
                    "url": data.get("url"),
                    "source": data.get("source"),
                    "offering_id": data.get("offering_id"),
                    "match_reasons": list(data.get("reasons") or []),
                    "observed_at": sig.observed_at,
                    "evidence_ids": [evidence_id],
                }
            )
        elif sig.signal_type == _DEMAND:
            phrase = data.get("phrase")
            vendor = data.get("vendor")
            detail = str(phrase or "")
            if vendor:
                detail = f"{detail} — required vendor: {vendor}"
            evidence_id = add_evidence(
                kind="demand",
                signal_id=sig.signal_id,
                detail=detail,
                url=data.get("job_url"),
                source=sig.source,
                observed_at=sig.observed_at,
                job_key=data.get("job_key"),
            )
            demands.append(
                {
                    "signal_id": sig.signal_id,
                    "job_key": data.get("job_key"),
                    "job_url": data.get("job_url"),
                    "phrase": phrase,
                    "vendor": vendor,
                    "department": data.get("department"),
                    "offering_id": data.get("offering_id"),
                    "match_reasons": list(data.get("reasons") or []),
                    "observed_at": sig.observed_at,
                    "evidence_ids": [evidence_id],
                }
            )

    # Cap the ACTIVE list only; needs/demands stay complete.
    ranked = sorted(active_all, key=lambda s: float(s.confidence or 0.0), reverse=True)
    shown = ranked if max_signals is None else ranked[: max(0, int(max_signals))]
    omitted = len(active_all) - len(shown)
    active_entries = [signal_entry(sig) for sig in shown]
    expired_entries = [signal_entry(sig) for sig in expired_all]

    by_type: dict[str, int] = {}
    for sig in shown:
        by_type[sig.signal_type] = by_type.get(sig.signal_type, 0) + 1
    counts = {
        "active": len(shown),
        "active_total": len(active_all),
        "active_omitted": omitted,
        "expired": len(expired_all),
        "by_type": by_type,
    }

    combos: list[dict] = []
    for combo in snapshot.combos:
        item = dict(combo)
        item["evidence_ids"] = [
            evidence_by_signal[sid]
            for sid in (combo.get("matched_signal_ids") or [])
            if sid in evidence_by_signal
        ]
        combos.append(item)

    plays = [
        {
            "play_id": play.play_id,
            "rank": play.rank,
            "urgency": play.urgency,
            "opener": play.opener,
            "t24": play.t24,
        }
        for play in snapshot.plays
    ]

    contact_list = list(snapshot.contacts)
    people = {
        "count": len(contact_list),
        "champion_count": sum(1 for c in contact_list if c.is_champion),
        "entries": [
            {
                "name": c.name,
                "title": c.title,
                "persona": c.persona,
                "seniority": c.seniority,
                "department": c.department,
                "is_champion": bool(c.is_champion),
            }
            for c in contact_list[:MAX_PEOPLE]
        ],
    }

    market = None
    if market_profile is not None:
        market = {
            "profile_id": getattr(market_profile, "profile_id", None) or market_profile_id,
            "seller": getattr(market_profile, "seller", None),
            "offering_ids": [
                getattr(offering, "offering_id", None)
                for offering in (getattr(market_profile, "offerings", ()) or ())
            ],
        }

    fit = None
    if snapshot.fit is not None:
        fit = {
            "multiplier": snapshot.fit.multiplier,
            "reasons": list(snapshot.fit.reasons or []),
            "disqualified": bool(snapshot.fit.disqualified),
            "disqualify_reason": snapshot.fit.disqualify_reason,
        }

    identity = {field: getattr(account, field, None) for field in _IDENTITY_FIELDS}
    # The snapshot is the authoritative calculation; prefer the persisted
    # account value when present, else the snapshot's, else null.
    if identity["tier"] is None:
        identity["tier"] = snapshot.tier.tier
    if identity["score"] is None:
        identity["score"] = snapshot.score.score
    if identity["buying_window"] is None:
        identity["buying_window"] = snapshot.tier.buying_window

    if isinstance(coverage, dict):
        coverage_rows = list(coverage.get("rows") or [])
        summary = coverage.get("summary") or coverage_summary(coverage_rows)
    else:
        coverage_rows = list(coverage or [])
        summary = coverage_summary(coverage_rows)

    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "name": account.name,
        "generated_at": generated_at if generated_at is not None else (
            today.isoformat() if today else None
        ),
        "invocation_id": invocation_id,
        "market_profile": market,
        "identity": identity,
        "fit": fit,
        "signals": {"active": active_entries, "expired": expired_entries, "counts": counts},
        "needs": needs,
        "demands": demands,
        "plays": plays,
        "combos": combos,
        "persona_framing": snapshot.persona_framing or None,
        "people": people,
        "jobs": jobs_summary,
        "coverage": {"rows": coverage_rows, "summary": summary},
        "gaps": list(gaps or []),
        "evidence_count": len(evidence),
        # The evidence index rides inside the dossier so the package is fully
        # self-contained (write_intel_package receives only the dossier).
        "evidence": evidence,
    }


def render_markdown(dossier: dict) -> str:
    """Render the dossier to markdown. Uses ONLY the dossier; invents nothing."""
    lines: list[str] = [
        f"# {_fmt(dossier.get('name'))}  ·  {_fmt(dossier.get('domain'))}",
        "",
        "## Identity and fit",
    ]
    identity = dossier.get("identity") or {}
    for field in _IDENTITY_FIELDS:
        lines.append(f"- {field}: {_fmt(identity.get(field))}")

    fit = dossier.get("fit")
    if fit is None:
        lines.append("- fit: — (not evaluated)")
    else:
        lines.append(f"- fit multiplier: {_fmt(fit.get('multiplier'))}")
        lines.append(f"- fit disqualified: {_fmt(fit.get('disqualified'))}")
        if fit.get("reasons"):
            lines.append(f"- fit reasons: {'; '.join(str(r) for r in fit['reasons'])}")
        lines.append(f"- disqualify reason: {_fmt(fit.get('disqualify_reason'))}")

    market = dossier.get("market_profile")
    lines.append(f"- market profile: {_fmt(market.get('profile_id') if market else None)}")
    lines.append(f"- persona framing: {_fmt(dossier.get('persona_framing'))}")
    lines.append(f"- generated at: {_fmt(dossier.get('generated_at'))}")

    lines += ["", "## Plays and combos"]
    plays = dossier.get("plays") or []
    if not plays:
        lines.append("- _No play assigned._")
    for play in plays:
        lines.append(
            f"### {_fmt(play.get('rank'))}. {_fmt(play.get('play_id'))}"
            f" (urgency {_fmt(play.get('urgency'))})"
        )
        lines.append(f"**Opener:** {_fmt(play.get('opener'))}")
        lines.append(f"**T24:** {_fmt(play.get('t24'))}")
        lines.append("")
    combos = dossier.get("combos") or []
    if not combos:
        lines.append("- _No combo fired._")
    for combo in combos:
        ids = ", ".join(combo.get("evidence_ids") or []) or "—"
        lines.append(
            f"- combo {_fmt(combo.get('id'))} (urgency {_fmt(combo.get('urgency'))})"
            f" — evidence: {ids}"
        )

    lines += ["", "## Signals by type"]
    counts = (dossier.get("signals") or {}).get("counts") or {}
    by_type = counts.get("by_type") or {}
    if not by_type:
        lines.append("- _No active signals._")
    for signal_type in sorted(by_type):
        lines.append(f"- {signal_type}: {by_type[signal_type]}")
    lines.append(f"- expired: {_fmt(counts.get('expired'))}")
    if counts.get("active_omitted"):
        lines.append(f"- active omitted by cap: {counts['active_omitted']}")

    lines += ["", "## Highest-confidence signals"]
    active = (dossier.get("signals") or {}).get("active") or []
    ranked = sorted(
        active, key=lambda s: float(s.get("confidence") or 0.0), reverse=True
    )[:MAX_MARKDOWN_SIGNALS]
    if not ranked:
        lines.append("- _none_")
    for sig in ranked:
        lines.append(
            f"### {_fmt(sig.get('signal_type'))}"
            f" ({_fmt(sig.get('observed_at'))}, conf {_fmt(sig.get('confidence'))})"
        )
        lines.append(f"- evidence: {_fmt(sig.get('evidence'))}")
        lines.append(f"- url: {_fmt(sig.get('url'))}")
        lines.append(
            f"- source: {_fmt(sig.get('source'))} · age_days: {_fmt(sig.get('age_days'))}"
        )
        lines.append(f"- evidence ids: {', '.join(sig.get('evidence_ids') or []) or '—'}")

    lines += ["", "## Operational needs"]
    needs = dossier.get("needs") or []
    if not needs:
        lines.append("- _none_")
    for need in needs:
        lines.append(f"- \"{_fmt(need.get('quote'))}\"")
        lines.append(
            f"  - offering: {_fmt(need.get('offering_id'))}"
            f" · matched: {_fmt(need.get('matched_phrase'))}"
        )
        lines.append(
            f"  - source: {_fmt(need.get('source'))} · url: {_fmt(need.get('url'))}"
        )
        lines.append(
            f"  - evidence ids: {', '.join(need.get('evidence_ids') or []) or '—'}"
        )

    lines += ["", "## Required-stack demands"]
    demands = dossier.get("demands") or []
    if not demands:
        lines.append("- _none_")
    for demand in demands:
        lines.append(
            f"- {_fmt(demand.get('phrase'))} — vendor: {_fmt(demand.get('vendor'))}"
        )
        lines.append(
            f"  - job: {_fmt(demand.get('job_key'))}"
            f" · department: {_fmt(demand.get('department'))}"
            f" · url: {_fmt(demand.get('job_url'))}"
        )
        lines.append(
            f"  - evidence ids: {', '.join(demand.get('evidence_ids') or []) or '—'}"
        )

    lines += ["", "## People"]
    people = dossier.get("people") or {}
    lines.append(
        f"- count: {_fmt(people.get('count'))} · champions: {_fmt(people.get('champion_count'))}"
    )
    for person in people.get("entries") or []:
        lines.append(
            f"- {_fmt(person.get('name'))} — {_fmt(person.get('title'))}"
            f" ({_fmt(person.get('persona'))}) champion={_fmt(person.get('is_champion'))}"
        )

    lines += ["", "## Jobs"]
    jobs = dossier.get("jobs")
    if jobs is None:
        lines.append("- _not supplied_")
    elif isinstance(jobs, dict):
        for key in sorted(jobs):
            lines.append(f"- {key}: {_fmt(jobs[key])}")
    elif isinstance(jobs, list):
        for item in jobs:
            lines.append(f"- {_fmt(item)}")
    else:
        lines.append(f"- {_fmt(jobs)}")

    lines += ["", "## Coverage and gaps"]
    coverage = dossier.get("coverage") or {}
    summary = coverage.get("summary") or {}
    if not summary:
        lines.append("- _no coverage rows_")
    for status in sorted(summary):
        lines.append(f"- status {status}: {summary[status]}")
    for row in coverage.get("rows") or []:
        lines.append(
            f"  - {_fmt(row.get('source'))} ({_fmt(row.get('key'))}):"
            f" {_fmt(row.get('status'))} — {_fmt(row.get('reason'))}"
        )
    gaps = dossier.get("gaps") or []
    if gaps:
        for gap in gaps:
            lines.append(f"- gap: {gap}")
    else:
        lines.append("- _no gaps reported_")

    lines += ["", "## Evidence index"]
    records = dossier.get("evidence") or []
    if not records:
        lines.append("- _none_")
    for record in records:
        lines.append(
            f"- {_fmt(record.get('evidence_id'))} [{_fmt(record.get('kind'))}]"
            f" {_fmt(record.get('detail'))} ({_fmt(record.get('url'))})"
        )
    lines.append("")
    return "\n".join(lines)


def render_prompt(dossier: dict) -> str:
    """Deterministic LLM query template. No network, no model call."""
    identity = dossier.get("identity") or {}
    fit = dossier.get("fit")
    if fit is None:
        fit_line = "unknown"
    else:
        fit_line = (
            f"multiplier {_fmt(fit.get('multiplier'))}"
            f" (disqualified={_fmt(fit.get('disqualified'))})"
        )
    return "\n".join(
        [
            "You are analysing ONE account from a supplied intel package.",
            f"Account: {_fmt(dossier.get('name'))} ({_fmt(dossier.get('domain'))}).",
            f"Buying window: {_fmt(identity.get('buying_window'))}. ICP fit: {fit_line}.",
            f"Evidence records in this package: {_fmt(dossier.get('evidence_count'))}.",
            "",
            "Use ONLY the supplied package. Do not use any outside knowledge and do",
            "not invent facts that are not present in the package.",
            "",
            "Fill these fields:",
            "- operational_need",
            "- buying_window",
            "- displacement_risk",
            "- expansion_signal",
            "- why_now",
            "",
            "Every claim must cite the evidence_id (for example [ev-0001]) or the",
            "signal_id behind it. If the package does not contain the evidence a",
            "field needs, answer \"unknown\" for that field rather than guessing.",
            "",
        ]
    )


def _slug(value: str) -> str:
    safe = [ch if (ch.isalnum() or ch in "._-") else "-" for ch in str(value)]
    return "".join(safe).strip("-") or "unknown"


def write_intel_package(dossier: dict, *, out_dir) -> dict:
    """Write the package into a fresh subdirectory of ``out_dir``.

    Writes ``manifest.json``, ``dossier.json``, ``dossier.md``,
    ``evidence.jsonl`` (one JSON record per line) and ``prompt.md``. The
    directory name carries the domain, a UTC timestamp and the invocation id,
    and is uniquified so two same-stamp writes never collide. Returns a dict of
    the written paths, including ``package_dir``.
    """
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)

    domain = _slug(dossier.get("domain") or "unknown-domain")
    invocation = _slug(str(dossier.get("invocation_id") or "noinvocation"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    package_dir = base / f"{domain}-{stamp}-{invocation}"
    suffix = 2
    while package_dir.exists():
        package_dir = base / f"{domain}-{stamp}-{invocation}-{suffix}"
        suffix += 1
    package_dir.mkdir(parents=True, exist_ok=False)

    evidence = list(dossier.get("evidence") or [])
    market = dossier.get("market_profile")
    manifest = {
        "kind": dossier.get("kind"),
        "schema_version": dossier.get("schema_version"),
        "domain": dossier.get("domain"),
        "name": dossier.get("name"),
        "market_profile_id": (market or {}).get("profile_id") if market else None,
        "generated_at": dossier.get("generated_at"),
        "invocation_id": dossier.get("invocation_id"),
        "evidence_count": dossier.get("evidence_count", len(evidence)),
        "files": {
            "manifest": "manifest.json",
            "dossier": "dossier.json",
            "markdown": "dossier.md",
            "evidence": "evidence.jsonl",
            "prompt": "prompt.md",
        },
    }

    paths: dict[str, str] = {"package_dir": str(package_dir)}

    manifest_path = package_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    paths["manifest"] = str(manifest_path)

    dossier_path = package_dir / "dossier.json"
    dossier_path.write_text(
        json.dumps(dossier, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    paths["dossier_json"] = str(dossier_path)

    markdown_path = package_dir / "dossier.md"
    markdown_path.write_text(render_markdown(dossier), encoding="utf-8", newline="\n")
    paths["dossier_md"] = str(markdown_path)

    evidence_path = package_dir / "evidence.jsonl"
    lines = [json.dumps(record, sort_keys=True) for record in evidence]
    evidence_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
    )
    paths["evidence_jsonl"] = str(evidence_path)

    prompt_path = package_dir / "prompt.md"
    prompt_path.write_text(render_prompt(dossier), encoding="utf-8", newline="\n")
    paths["prompt_md"] = str(prompt_path)

    return paths
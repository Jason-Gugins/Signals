"""Derive hiring / geo / migration signals from a jobs window. PURE."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from src.core.textutil import guess_seniority, to_iso_date
from src.sources.base import SignalCandidate


@dataclass
class JobsWindow:
    domain: str
    jobs: list[dict]
    prior_countries: set[str]
    prior_departments: set[str]
    baseline_open_by_dept: dict[str, int]


MIGRATION_PATTERNS = [
    re.compile(
        r"migrat\w+\s+(?:from|off(?:\s+of)?)\s+(?P<from>[A-Z][\w .+-]{2,30})\s+to\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
    re.compile(
        r"replac\w+\s+(?P<from>[A-Z][\w .+-]{2,30})\s+with\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
    re.compile(r"sunset\w*\s+(?P<from>[A-Z][\w .+-]{2,30})", re.I),
    re.compile(
        r"consolidat\w+\s+.{0,40}\bonto\s+(?P<to>[A-Z][\w .+-]{2,30})",
        re.I,
    ),
]

PROJECT_PATTERNS = [
    re.compile(r"\b(?:we are|we're)\s+(?:building|standing up|rolling out)\s+(?P<what>[A-Za-z][\w .+-]{2,40})", re.I),
]

# ── job-text vendor extraction (roadmap Task 12) ────────────────────────────
# Deepening for tech_migration_mentioned / required-stack evidence. Vendor
# matching is case-insensitive against a KNOWN-NAME allowlist (the techstack
# fingerprint vendor keys plus a small supplemental set) instead of raw
# capitalized-name regexes, so lowercase "salesforce"/"snowflake" in job text
# now matches. Precision guards mirror the news classifier's common-word
# handling (news/classify.py): word-boundary matching only, and names that
# collide with everyday English require a B2B context word in the description.

_RELAXED_MIGRATION_PATTERNS = [
    # Same verbs as MIGRATION_PATTERNS without the capital-letter requirement;
    # captured names are only trusted when they resolve to a known vendor.
    re.compile(
        r"migrat\w+\s+(?:from|off(?:\s+of)?)\s+(?P<from>[\w .+-]{2,30}?)\s+to\s+(?P<to>[\w .+-]{2,30})",
        re.I,
    ),
    re.compile(r"replac\w+\s+(?P<from>[\w .+-]{2,30}?)\s+with\s+(?P<to>[\w .+-]{2,30})", re.I),
    re.compile(r"sunset\w*\s+(?P<from>[\w .+-]{2,30})", re.I),
    re.compile(r"consolidat\w+\s+.{0,40}\bonto\s+(?P<to>[\w .+-]{2,30})", re.I),
]

_REQUIRED_STACK_PATTERNS = [
    re.compile(r"\bexperience\s+(?:with|using|in)\s+([\w +/#&-]{2,40})", re.I),
    re.compile(r"\bproficien(?:cy|t)\s+(?:with|in)\s+([\w +/#&-]{2,40})", re.I),
    re.compile(r"\b([\w +/#&-]{2,30}?)\s+experience\s+(?:is\s+)?(?:required|preferred|necessary|mandatory|a\s+must)", re.I),
]

_STACK_PATTERNS = [
    re.compile(r"\bbuilt\s+on\s+(?:top\s+of\s+)?([\w +/#&-]{3,60})", re.I),
    re.compile(r"\b(?:runs?|running)\s+on\s+([\w +/#&-]{3,60})", re.I),
    re.compile(r"\bpowered\s+by\s+([\w +/#&-]{3,60})", re.I),
    re.compile(r"\bour\s+(?:tech|technology|data)?\s*stack\s+(?:is|includes?)\s+([\w +/#&-]{3,60})", re.I),
    re.compile(r"\bwe\s+(?:use|rely\s+on)\s+([\w +/#&-]{2,40})", re.I),
]

# B2B SaaS names that job posts use but fingerprints.yaml does not track.
_SUPPLEMENTAL_VENDORS = frozenset({
    "asana", "monday", "notion", "slack", "jira", "confluence", "trello",
    "airtable", "figma", "databricks", "tableau", "looker", "netsuite",
    "servicenow", "gorgias", "salesloft", "outreach", "braze", "eloqua",
})
# Vendor keys that collide with everyday job-post jargon; never matched.
# ("gtm" is Google Tag Manager in fingerprints but "go-to-market" in prose.)
_EXCLUDED_VENDORS = frozenset({"gtm"})
# Common-English vendor names: a bare mention only counts when the
# description also carries B2B context ("monday" the day vs monday.com).
_AMBIGUOUS_VENDOR_NAMES = frozenset({
    "monday", "asana", "notion", "workday", "outreach", "segment", "vector",
})
_B2B_CONTEXT_WORDS = (
    "crm", "platform", "software", "integration", "saas", "stack", "tool",
    "tools", "tooling", "vendor", "suite", "erp", "hris", "dashboard",
    "analytics", "api", "workflow", "migration", "migrate", "system",
    "systems", "solution", "database", "infrastructure", "app", "application",
)


def _vendor_vocab() -> list[tuple[str, re.Pattern]]:
    """Known vendor names (casefolded, longest first) with word-boundary matchers.

    Derived from the techstack fingerprint vendor keys (function-local import:
    this module stays free of file-I/O helpers) plus a supplemental set of B2B
    SaaS names. Multi-word keys gain a spaced variant ("google_workspace" also
    matches "google workspace"). Best-effort: if the fingerprint rules cannot
    be loaded, the supplemental set still applies.
    """
    names: set[str] = set(_SUPPLEMENTAL_VENDORS)
    try:
        from src.sources.techstack.fingerprint import load_fingerprint_rules

        vendors = (load_fingerprint_rules() or {}).get("vendors") or {}
        for key, spec in vendors.items():
            fold = (key or "").casefold()
            if not fold:
                continue
            names.add(fold)
            if "_" in fold:
                names.add(fold.replace("_", " "))
            display = (spec or {}).get("display")
            if display:
                names.add(display.casefold())
    except Exception:
        pass  # vocabulary is best-effort; supplemental names still apply
    names -= _EXCLUDED_VENDORS
    vocab = [(n, re.compile(rf"\b{re.escape(n)}\b", re.I)) for n in names]
    vocab.sort(key=lambda t: -len(t[0]))
    return vocab


def _vendors_in(text: str, vocab: list[tuple[str, re.Pattern]]) -> list[tuple[int, int, str]]:
    """All non-overlapping known-vendor hits in text, in text order.

    Returns (start, end, matched_text). Longer names win at the same position
    and nested hits inside a kept span are dropped.
    """
    hits: list[tuple[int, int, str]] = []
    for _name, rx in vocab:
        for m in rx.finditer(text):
            hits.append((m.start(), m.end(), m.group(0)))
    hits.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    out: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, raw in hits:
        if start < last_end:
            continue  # nested inside an already-kept longer span
        out.append((start, end, raw))
        last_end = end
    return out


def _first_vendor(span: str | None, vocab: list[tuple[str, re.Pattern]]) -> str | None:
    if not span:
        return None
    hits = _vendors_in(span, vocab)
    return hits[0][2] if hits else None


def _stack_mentions(desc: str, vocab: list[tuple[str, re.Pattern]]) -> list[str]:
    """Vendors named in stack-bearing phrases plus bare allowlist mentions.

    Ambiguous common-word names (e.g. "monday") count as bare mentions only
    when a B2B context word co-occurs in the description; mentions inside an
    explicit stack phrase are already context-legitimized.
    """
    low = desc.casefold()
    has_ctx = any(re.search(rf"\b{re.escape(w)}\b", low) for w in _B2B_CONTEXT_WORDS)
    hits: dict[int, str] = {}
    for pat in _STACK_PATTERNS:
        for m in pat.finditer(desc):
            for pos, _end, raw in _vendors_in(m.group(1), vocab):
                hits.setdefault(m.start(1) + pos, raw)
    for pos, _end, raw in _vendors_in(desc, vocab):
        if raw.casefold() in _AMBIGUOUS_VENDOR_NAMES and not has_ctx:
            continue
        hits.setdefault(pos, raw)
    out: list[str] = []
    seen: set[str] = set()
    for _pos, raw in sorted(hits.items()):
        fold = raw.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        out.append(raw)
    return out


def _required_stack(desc: str, vocab: list[tuple[str, re.Pattern]]) -> list[str]:
    """Canonical (casefolded) vendor names demanded by required-stack phrasing."""
    hits: dict[int, str] = {}
    for pat in _REQUIRED_STACK_PATTERNS:
        for m in pat.finditer(desc):
            for pos, _end, raw in _vendors_in(m.group(1), vocab):
                hits.setdefault(m.start(1) + pos, raw.casefold())
    out: list[str] = []
    seen: set[str] = set()
    for _pos, name in sorted(hits.items()):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _migration_pairs(desc: str, vocab: list[tuple[str, re.Pattern]]) -> list[tuple[str, str]]:
    """All migration (from, to) pairs in a description, deduped casefold-wise.

    Capitalized MIGRATION_PATTERNS keep their raw captures (names not in the
    allowlist fall back to the captured text); relaxed patterns only trust
    allowlist-backed names, so "migrating to the cloud" still never matches.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for old, pat in (
        *[(True, p) for p in MIGRATION_PATTERNS],
        *[(False, p) for p in _RELAXED_MIGRATION_PATTERNS],
    ):
        for m in pat.finditer(desc):
            gd = m.groupdict()
            frm_hit = _first_vendor(gd.get("from"), vocab)
            to_hit = _first_vendor(gd.get("to"), vocab)
            if old:
                frm = frm_hit or (gd.get("from") or "").strip()
                to = to_hit or (gd.get("to") or "").strip()
            else:
                if not frm_hit and not to_hit:
                    continue
                frm, to = frm_hit or "", to_hit or ""
            key = (frm.casefold(), to.casefold())
            if key == ("", "") or key in seen:
                continue
            seen.add(key)
            pairs.append((frm, to))
    return pairs


_LEAD = {"c_level", "vp", "director", "head"}


def _cite(jobs: list[dict]) -> dict:
    titles = [j.get("title") for j in jobs if j.get("title")][:3]
    urls = [j.get("url") for j in jobs if j.get("url")][:3]
    return {"job_titles": titles, "job_urls": urls}


def analyze_jobs(w: JobsWindow, *, today: date, cfg: dict) -> list[SignalCandidate]:
    surge_min = int(cfg.get("surge_min_roles", 5))
    surge_days = int(cfg.get("surge_window_days", 60))
    iso_week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    iso_month = today.strftime("%Y-%m")
    cutoff_surge = today - timedelta(days=surge_days)
    cutoff_office = today - timedelta(days=90)
    out: list[SignalCandidate] = []

    open_jobs = [j for j in w.jobs if not j.get("closed_at")]
    by_dept: dict[str, list[dict]] = defaultdict(list)
    by_country: dict[str, list[dict]] = defaultdict(list)
    by_city: dict[str, list[dict]] = defaultdict(list)
    for j in open_jobs:
        if j.get("department"):
            by_dept[j["department"]].append(j)
        if j.get("country"):
            by_country[j["country"]].append(j)
        if j.get("city"):
            posted = to_iso_date(j.get("posted_at"))
            if posted and date.fromisoformat(posted) >= cutoff_office:
                by_city[j["city"]].append(j)

    # hiring_surge
    for dept, rows in by_dept.items():
        recent = []
        for j in rows:
            posted = to_iso_date(j.get("posted_at"))
            if posted and date.fromisoformat(posted) >= cutoff_surge:
                recent.append(j)
        baseline = w.baseline_open_by_dept.get(dept, 0)
        ratio_hit = baseline > 0 and len(rows) >= 1.5 * baseline and (len(rows) - baseline) >= 4
        count_hit = len(recent) >= surge_min
        if count_hit or ratio_hit:
            out.append(
                SignalCandidate(
                    signal_type="hiring_surge",
                    observed_at=today.isoformat(),
                    natural_key=f"surge:{dept}:{iso_week}",
                    title=f"{dept} hiring surge",
                    confidence=0.85,
                    evidence_data={**_cite(recent or rows), "department": dept, "job_count": len(recent or rows)},
                )
            )

    # leadership
    for j in open_jobs:
        if guess_seniority(j.get("title")) in _LEAD:
            out.append(
                SignalCandidate(
                    signal_type="leadership_job_open",
                    observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                    natural_key=f"job:{j.get('job_key') or j.get('external_id')}",
                    title=j.get("title"),
                    url=j.get("url"),
                    confidence=0.9,
                    evidence_data={**_cite([j]), "department": j.get("department")},
                )
            )

    # department expansion
    for dept, rows in by_dept.items():
        if len(rows) >= 2 and dept not in w.prior_departments:
            out.append(
                SignalCandidate(
                    signal_type="department_expansion",
                    observed_at=today.isoformat(),
                    natural_key=f"deptnew:{dept}:{iso_month}",
                    title=f"New {dept} department",
                    confidence=0.75,
                    evidence_data={**_cite(rows), "department": dept},
                )
            )

    # new geo
    for country, rows in by_country.items():
        if country not in w.prior_countries:
            out.append(
                SignalCandidate(
                    signal_type="new_geo",
                    observed_at=today.isoformat(),
                    natural_key=f"geo:{country}:{iso_month}",
                    title=f"Hiring in {country}",
                    confidence=0.8,
                    evidence_data={**_cite(rows), "country": country},
                )
            )

    # office open
    for city, rows in by_city.items():
        if len(rows) >= 3:
            out.append(
                SignalCandidate(
                    signal_type="office_open",
                    observed_at=today.isoformat(),
                    natural_key=f"office:{city}:{iso_month}",
                    title=f"Office activity in {city}",
                    confidence=0.7,
                    evidence_data={**_cite(rows), "city": city},
                )
            )

    # migration + scoop (Task 12: all matches per job, aggregated evidence)
    vocab = _vendor_vocab()
    for j in open_jobs:
        desc = j.get("description") or ""
        if not desc.strip():
            continue
        pairs = _migration_pairs(desc, vocab)
        mentioned = _stack_mentions(desc, vocab)
        required = _required_stack(desc, vocab)
        # tech_migration_mentioned asserts a MIGRATION — bare stack mentions
        # and required-stack text don't assert one, and the evidence template
        # would render "migrating from  to X" with an empty from_tech. Emit
        # only on real migration pairs; mentions/required still enrich the
        # evidence of candidates that do fire. (The scoop scan below must run
        # for EVERY job — only the migration emission is gated.)
        if pairs:
            frm, to = pairs[0]
            conf = 0.7
            covered = {frm.casefold(), to.casefold()} - {""}
            also: list[dict] = []
            for p2 in pairs[1:]:
                also.append({"from": p2[0], "to": p2[1]})
                covered.update({p2[0].casefold(), p2[1].casefold()} - {""})
            for raw in mentioned:
                if raw.casefold() not in covered:
                    also.append({"vendor": raw})
                    covered.add(raw.casefold())
            ev = {**_cite([j]), "from_tech": frm, "to_tech": to}
            if also:
                ev["also_mentioned"] = also
            if required:
                ev["required_stack"] = required
            out.append(
                SignalCandidate(
                    signal_type="tech_migration_mentioned",
                    observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                    natural_key=f"mig:{j.get('job_key') or j.get('external_id')}:{frm}:{to}",
                    title=j.get("title"),
                    url=j.get("url"),
                    confidence=conf,
                    evidence_data=ev,
                )
            )
        for pat in PROJECT_PATTERNS:
            if pat.search(desc):
                ev = _cite([j])
                if required:
                    ev["required_stack"] = required
                out.append(
                    SignalCandidate(
                        signal_type="internal_project_scoop",
                        observed_at=to_iso_date(j.get("posted_at")) or today.isoformat(),
                        natural_key=f"scoop:{j.get('job_key') or j.get('external_id')}",
                        title=j.get("title"),
                        url=j.get("url"),
                        confidence=0.6,
                        evidence_data=ev,
                    )
                )
                break

    out.sort(key=lambda c: (c.signal_type, c.natural_key))
    return out

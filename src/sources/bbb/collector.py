"""BBB business-profile source. Pure parser + adapter (spike verdict: GO plain-fetch).

BBB profile pages (``https://www.bbb.org/us/<st>/<city>/profile/<category>/<slug>-<bbb>-<id>``)
are SERVER-RENDERED HTML (P2 source spike, Task 6: Avalara profile fetched 200 at
135.7 KB with curl_cffi chrome impersonation, no anti-bot challenge). The parse
surface verified on the live capture:

- ``<script type="application/ld+json">`` — array whose LocalBusiness entry
  carries the canonical ``name`` (business identity).
- ``<dt>/<dd>`` facts table under ``div.bpr-details`` — Local BBB, BBB File
  Opened, Business Started, Business Incorporated, Type of Entity, Alternate
  Names (multi-``<dd>``), Business Management.
- ``div.bpr-header-accreditation-rating[data-accredited]`` — accreditation flag.
- ``span.bpr-header-rating`` — letter grade ("A+").

BBB facts are IDENTITY EVIDENCE and REPUTATION EVIDENCE: ``parse_bbb_profile``
returns a plain facts dict and the adapter raises candidates in exactly two
cases —

- an alternate name cross-resolves (via ``AccountRegistry.resolve(name=...)``)
  to a DIFFERENT account than the profiled one (identity), or
- the letter grade DROPPED or accreditation was LOST versus the observation
  persisted on the account from the previous cycle (``reputation_drop``).

The rating baseline lives on the account itself: every successful profile
parse merges ``bbb_rating`` / ``bbb_accredited`` / ``bbb_observed_at`` into
``account.extra_data`` through the ``AccountRegistry`` the runner injects
into parse ``task_meta`` (``meta.setdefault("registry", ...)``), so the next
cycle can diff against it. Merged, never replaced — unrelated keys (the
seeded ``bbb_url``) survive.

LIMITATIONS (per spike):
- Complaint lists are NOT probed (``/complaints`` paginates; out of budget).
- Profile URLs are NOT derivable from domain+name alone — the URL slug embeds
  BBB region code and internal business id. The seed CSV must carry the full
  ``bbb_url`` via account ``extra_data``; ``plan()`` emits nothing without it.
"""

from __future__ import annotations

import json
from src.core.textutil import slugify
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

__all__ = ["parse_bbb_profile", "BbbProfileSource", "grade_ordinal", "BBB_GRADE_SCALE"]

# BBB letter-grade scale, best -> worst. The index IS the ordinal: a downgrade
# is any step toward a HIGHER ordinal. "NR" (no rating) and anything unknown
# deliberately maps to None — an unrankable grade never drives a delta.
BBB_GRADE_SCALE = ("A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F")


def grade_ordinal(grade) -> int | None:
    """Ordinal of a BBB letter grade on the A+..F scale (higher = worse). PURE.

    Unknown, blank, or non-string grades (BBB's "NR", markup drift) return
    None so callers can never compare against a guessed position.
    """
    if not isinstance(grade, str):
        return None
    token = grade.strip().upper()
    return BBB_GRADE_SCALE.index(token) if token in BBB_GRADE_SCALE else None


def _dt_dd_facts(soup) -> dict[str, list[str]]:
    """Map the Business Details <dt>/<dd> table: label -> list of value texts."""
    facts: dict[str, list[str]] = {}
    for div in soup.select(".bpr-details-dl-data"):
        dt = div.find("dt")
        if dt is None:
            continue
        label = " ".join(dt.get_text(" ", strip=True).split()).rstrip(":")
        values = []
        for dd in div.find_all("dd"):
            text = " ".join(dd.get_text(" ", strip=True).split())
            if text:
                values.append(text)
        if label and values:
            facts.setdefault(label, []).extend(values)
    return facts


def parse_bbb_profile(html: str) -> dict:
    """Parse a BBB profile page into a facts dict. PURE.

    bs4 is imported lazily inside the function (marketplace-parser style):
    no network, no I/O, no clock reads.

    Returns ``{business_name, alternate_names, entity_type, accredited,
    accredited_present, bbb_rating, file_opened}``. ``alternate_names`` is a
    list (possibly empty); scalar fields are ``None`` when the markup is
    absent. ``accredited_present`` distinguishes "genuinely not accredited"
    (``data-accredited`` found, false) from "markup absent" (the parser's
    ``accredited=False`` default) so the delta logic never reads an
    accreditation loss out of a page that simply stopped rendering the flag.
    """
    from bs4 import BeautifulSoup

    out: dict = {
        "business_name": None,
        "alternate_names": [],
        "entity_type": None,
        "accredited": False,
        "accredited_present": False,
        "bbb_rating": None,
        "file_opened": None,
    }
    if not html:
        return out

    soup = BeautifulSoup(html, "lxml")

    # --- business_name: ld+json LocalBusiness --------------------------------
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("@type") == "LocalBusiness":
                name = entry.get("name")
                if name:
                    out["business_name"] = name.strip()
                    break
        if out["business_name"]:
            break

    # --- <dt>/<dd> facts ------------------------------------------------------
    facts = _dt_dd_facts(soup)
    if facts.get("Type of Entity"):
        out["entity_type"] = facts["Type of Entity"][0]
    if facts.get("BBB File Opened"):
        out["file_opened"] = facts["BBB File Opened"][0]
    for name in facts.get("Alternate Names", []):
        if name and name not in out["alternate_names"]:
            out["alternate_names"].append(name)

    # --- accreditation + rating (bpr-header-*) -------------------------------
    accred = soup.select_one("div.bpr-header-accreditation-rating[data-accredited]")
    if accred is not None:
        out["accredited_present"] = True
        out["accredited"] = str(accred.get("data-accredited", "")).strip().lower() == "true"
    rating = soup.select_one(".bpr-header-rating")
    if rating is not None:
        text = rating.get_text(strip=True)
        out["bbb_rating"] = text or None

    return out


def _rating_delta(account, facts: dict, registry, today, *, doc_url=None):
    """Diff this cycle's BBB facts against the account's stored baseline.

    Persistence mechanism: the runner injects the ``AccountRegistry`` into
    parse ``task_meta`` (``meta.setdefault("registry", ...)`` in
    ``CollectorRunner._run_pair``), so the adapter reads the stored account
    (``registry.get``) and writes through the SAME ``registry.upsert``
    account-upsert path the Form D identity flow uses — no runner changes.

    ``extra_data`` is MERGED, never replaced: only ``bbb_rating`` /
    ``bbb_accredited`` / ``bbb_observed_at`` are written, and only from facts
    actually present in this cycle's markup (a page that stops rendering the
    rating span or the accreditation flag never erases the stored baseline —
    the adapter-wiring lesson: sparse upserts must not NULL stored fields).

    Emission rules (``reputation_drop``, 720h cadence = per-cycle diff):
    letter-grade downgrade (any step down the A+..F scale) or accreditation
    loss (True -> False, only when the flag was actually rendered this
    cycle). Improvements and no change emit nothing; the FIRST observation
    stores the baseline and emits nothing (no baseline, never guess).

    Returns at most one SignalCandidate (a downgrade and a simultaneous
    accreditation loss are one event, one natural key per month).
    """
    base = registry.get(account.domain) or account
    original = dict(getattr(base, "extra_data", None) or {})
    stored = dict(original)
    prev_rating = stored.get("bbb_rating")
    prev_accredited = stored.get("bbb_accredited")

    new_rating = facts.get("bbb_rating")
    new_accredited = facts.get("accredited")
    accredited_seen = bool(facts.get("accredited_present"))

    old_o, new_o = grade_ordinal(prev_rating), grade_ordinal(new_rating)
    downgrade = old_o is not None and new_o is not None and new_o > old_o
    lost_accreditation = (
        prev_accredited is True and accredited_seen and new_accredited is False
    )

    cand = None
    if (downgrade or lost_accreditation) and today:
        parts = []
        if downgrade:
            parts.append(f"BBB letter grade fell from {prev_rating} to {new_rating}")
        if lost_accreditation:
            parts.append("BBB accreditation lost")
        cand = SignalCandidate(
            signal_type="reputation_drop",
            observed_at=today,
            natural_key=f"bbbdrop:{account.domain}:{today[:7]}",
            title=f"BBB {prev_rating or '?'} → {new_rating or '?'}",
            summary="; ".join(parts),
            url=doc_url,
            confidence=0.7,
            domain_override=account.domain,
            evidence_data={
                "old_rating": prev_rating,
                "new_rating": new_rating,
                "accredited_before": prev_accredited,
                "accredited_after": new_accredited,
            },
        )

    changed = False
    if new_rating is not None:
        stored["bbb_rating"] = new_rating
        if today:
            stored["bbb_observed_at"] = today
        changed = True
    if accredited_seen:
        stored["bbb_accredited"] = bool(new_accredited)
        changed = True
    if changed and stored != original:
        base.extra_data = stored
        registry.upsert(base)
    return cand


@register
class BbbProfileSource(SourceAdapter):
    key = "bbb_profile"
    tier = "http"
    cadence_hours = 720  # 30 days: BBB profile facts change slowly
    requires: tuple[str, ...] = ()
    emits = ("intent_3rd_topic", "reputation_drop")

    def plan(self, account, cursor) -> list:
        """One FetchTask per account, gated on ``extra_data['bbb_url']``.

        BBB profile URLs embed the BBB region code and internal business id and
        are NOT derivable from domain+name alone — the seed CSV must carry them
        via ``extra_data['bbb_url']``. Accounts without a seeded URL plan
        nothing (do not guess/construct profile URLs).
        """
        from src.core.models import Account

        extra = getattr(account, "extra_data", None) or {}
        bbb_url = extra.get("bbb_url")
        if not bbb_url:
            return []
        return [FetchTask(source=self.key, url=bbb_url, domain=account.domain)]

    def parse(self, doc, account, task_meta) -> list:
        """Rating delta + cross-account alias resolution.

        Both flows run through ``task_meta['registry']`` (the AccountRegistry
        injected by the runner): ``_rating_delta`` persists the observation on
        the account and maybe emits ``reputation_drop``; alternate names are
        resolved via ``registry.resolve(name=...)`` and emit one
        SignalCandidate per name that resolves to a DIFFERENT account. No
        registry in task_meta: ``[]`` — nothing can be resolved or persisted.
        """
        registry = (task_meta or {}).get("registry")
        if registry is None:
            return []

        html = doc.body.decode("utf-8", errors="replace") if isinstance(doc.body, bytes) else (doc.body or "")
        facts = parse_bbb_profile(html)

        out = []
        today = (task_meta.get("today") or (doc.fetched_at or ""))[:10] or None
        delta = _rating_delta(account, facts, registry, today, doc_url=doc.url)
        if delta is not None:
            out.append(delta)

        business_id = slugify(facts.get("business_name") or (account.domain or ""))

        for alt in facts.get("alternate_names", []):
            try:
                hit = registry.resolve(name=alt)
            except (KeyError, ValueError):
                # Bad alias rows / schema drift — skip this name, but let
                # programming errors (TypeError etc.) surface.
                continue
            if hit is None:
                continue
            other_domain = getattr(hit, "domain", None)
            if not other_domain or other_domain == account.domain:
                continue  # same account (or self) — not cross-account evidence
            out.append(
                SignalCandidate(
                    signal_type="intent_3rd_topic",
                    observed_at=(task_meta.get("today") or (doc.fetched_at or ""))[:10] or None,
                    natural_key=f"bbba:{business_id}:{slugify(alt)}",
                    title=f"BBB profile of {facts.get('business_name')} lists alternate name {alt!r} (also tracked as {other_domain})",
                    summary=f"BBB alternate name {alt!r} resolves to tracked account {other_domain}",
                    url=doc.url,
                    confidence=0.7,
                    domain_override=account.domain,
                    evidence_data={
                        "alternate_name": alt,
                        "resolved_domain": other_domain,
                        "facts": facts,
                    },
                )
            )
        return out

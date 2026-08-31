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

BBB facts are IDENTITY EVIDENCE, not intent signals: ``parse_bbb_profile``
returns a plain facts dict and the adapter raises a candidate ONLY when an
alternate name cross-resolves (via ``AccountRegistry.resolve(name=...)``) to a
DIFFERENT account than the profiled one — i.e. the profiled business operates
under a name already tracked as a distinct account.

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

__all__ = ["parse_bbb_profile", "BbbProfileSource"]


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
    bbb_rating, file_opened}``. ``alternate_names`` is a list (possibly empty);
    scalar fields are ``None`` when the markup is absent.
    """
    from bs4 import BeautifulSoup

    out: dict = {
        "business_name": None,
        "alternate_names": [],
        "entity_type": None,
        "accredited": False,
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
        out["accredited"] = str(accred.get("data-accredited", "")).strip().lower() == "true"
    rating = soup.select_one(".bpr-header-rating")
    if rating is not None:
        text = rating.get_text(strip=True)
        out["bbb_rating"] = text or None

    return out


@register
class BbbProfileSource(SourceAdapter):
    key = "bbb_profile"
    tier = "http"
    cadence_hours = 720  # 30 days: BBB profile facts change slowly
    requires: tuple[str, ...] = ()
    emits = ("intent_3rd_topic",)

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
        """Cross-account alias resolution. PURE.

        For each alternate name on the profile, resolve it through
        ``task_meta['registry'].resolve(name=...)``. Emit one SignalCandidate
        per alternate name that resolves to a DIFFERENT account. No registry
        in task_meta, or no cross-account match: ``[]``.
        """
        from src.core.models import Account

        registry = (task_meta or {}).get("registry")
        if registry is None:
            return []

        html = doc.body.decode("utf-8", errors="replace") if isinstance(doc.body, bytes) else (doc.body or "")
        facts = parse_bbb_profile(html)
        business_id = slugify(facts.get("business_name") or (account.domain or ""))

        out = []
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

"""T10 — BBB relocation signal: headquarters address delta.

The ``relocation`` type (``config/signals.yaml``) was registered but dead:
``parse_bbb_profile`` extracted no address, so nothing could diff. This wave
wires it on the address parse surface verified OFFLINE against the real P2
spike capture (``data/probe/p2_spike_bbb-avalara.html``, 200 OK SSR — the
sibling ``p2_spike_bbb-profile.html`` is the BBB 404 page and was probed
first: zero address markup, no selectors guessed; verdict recorded in
``data/probe/P2_SOURCE_SPIKE.md`` "BBB address parse surface (T10)"):

- JSON-LD ``LocalBusiness.address`` ``PostalAddress`` (structured pieces),
- ``div.bpr-overview-address`` rendered block (fallback).

The emitter mirrors ``_rating_delta`` exactly: baseline
``extra_data['bbb_address']`` read/written through the SAME
registry-injected ``task_meta`` mechanism (NOT the runner prev-doc seam);
first observation stores the baseline and emits NOTHING; a changed one-line
address emits exactly one ``relocation`` keyed on the hash of the NEW
address (idempotent per change — re-parse dedupes); unchanged or
missing-on-either-side emits nothing and never compares None vs value.

Covers the pure parser on the real page shape (fixture carries the capture's
ld+json script and overview-address block VERBATIM), the delta rules through
the real adapter ``parse``, and a ``normalize_batch`` + ``SignalStore``
persistence test pinning that the Signal row lands exactly once and is
idempotent on re-upsert (``test_security_breach_persistence`` pattern).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.core.textutil import stable_id
from src.identity.registry import AccountRegistry
from src.signals.evidence import render_evidence
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.bbb import BbbProfileSource, parse_bbb_profile

FIXTURE = Path(__file__).parent / "fixtures" / "bbb" / "bbb_avalara_address_snippet.html"
BBB_URL = "https://www.bbb.org/us/wa/seattle/profile/computer-software-developers/avalara-inc-1296-22018273"
TODAY = "2026-09-05"

# The real capture's address, verbatim, and the simulated post-move page.
OLD_ADDRESS = "906 Alaskan Way # 500, Seattle, WA 98104-1010"
NEW_ADDRESS = "1200 112th Ave NE, Bellevue, WA 98004"

_LDJSON_ADDR = (
    '"address":{"@type":"PostalAddress","addressLocality":"Seattle",'
    '"addressRegion":"WA","postalCode":"98104-1010","addressCountry":"USA",'
    '"streetAddress":"906 Alaskan Way # 500"}'
)
_LDJSON_ADDR_MOVED = (
    '"address":{"@type":"PostalAddress","addressLocality":"Bellevue",'
    '"addressRegion":"WA","postalCode":"98004","addressCountry":"USA",'
    '"streetAddress":"1200 112th Ave NE"}'
)
_HTML_STREET = '<p class="bds-body" translate="no">906 Alaskan Way # 500</p>'
_HTML_STREET_MOVED = '<p class="bds-body" translate="no">1200 112th Ave NE</p>'
_HTML_CITYZIP = (
    '<p class="bds-body" translate="no">Seattle<!-- -->, <!-- -->WA'
    '<!-- --> <!-- -->98104-1010</p>'
)
_HTML_CITYZIP_MOVED = (
    '<p class="bds-body" translate="no">Bellevue<!-- -->, <!-- -->WA'
    '<!-- --> <!-- -->98004</p>'
)
_HTML_BLOCK = (
    '<div class="bpr-overview-address">' + _HTML_STREET + _HTML_CITYZIP + "</div>"
)
_LDJSON_RE = re.compile(r'<script type="application/ld\+json">.*?</script>', re.S)


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _moved(html: str) -> str:
    """Fixture surgery: relocate HQ, keeping BOTH address surfaces real."""
    out = (
        html.replace(_LDJSON_ADDR, _LDJSON_ADDR_MOVED)
        .replace(_HTML_STREET, _HTML_STREET_MOVED)
        .replace(_HTML_CITYZIP, _HTML_CITYZIP_MOVED)
    )
    assert out != html, "address markup not found in fixture"
    return out


def _ldjson_stripped(html: str) -> str:
    """Fixture surgery: a page whose JSON-LD carries no data (drift) — the
    rendered overview block remains the only address surface."""
    out = _LDJSON_RE.sub("", html)
    assert out != html and "bpr-overview-address" in out
    return out


def _addressless(html: str) -> str:
    """Fixture surgery: a profile page rendering NO address on either surface."""
    out = _LDJSON_RE.sub("", html).replace(_HTML_BLOCK, "")
    # (the fixture's header comment mentions the block by name — assert on
    # the actual div, not the bare substring)
    assert out != html and '<div class="bpr-overview-address">' not in out
    return out


def _doc(html: str) -> Document:
    return Document(
        doc_id="bbb-reloc-doc",
        source="bbb_profile",
        url=BBB_URL,
        body=html.encode("utf-8"),
    )


def _registry(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "reg.db"))


def _account(registry: AccountRegistry) -> Account:
    return registry.upsert(
        Account(domain="avalara.com", name="Avalara Inc", extra_data={"bbb_url": BBB_URL})
    )


def _parse(registry, account, html):
    return BbbProfileSource().parse(_doc(html), account, {"registry": registry, "today": TODAY})


def _relocs(cands) -> list:
    return [c for c in cands if c.signal_type == "relocation"]


# ── pure parser: real capture shape ──────────────────────────────────────────


def test_parse_extracts_address_pieces_from_real_capture_shape():
    facts = parse_bbb_profile(_html())

    assert facts["address_street"] == "906 Alaskan Way # 500"
    assert facts["address_city"] == "Seattle"
    assert facts["address_state"] == "WA"
    assert facts["address_zip"] == "98104-1010"
    # The React comment separators (<!-- -->) must not leak into the text.
    assert facts["address"] == OLD_ADDRESS
    assert facts["business_name"] == "Avalara Inc"


def test_parse_falls_back_to_overview_address_block_without_ldjson():
    facts = parse_bbb_profile(_ldjson_stripped(_html()))

    assert facts["business_name"] is None  # ld+json gone: name unparseable too
    assert facts["address_street"] == "906 Alaskan Way # 500"
    assert facts["address_city"] == "Seattle"
    assert facts["address_state"] == "WA"
    assert facts["address_zip"] == "98104-1010"
    assert facts["address"] == OLD_ADDRESS


def test_parse_without_address_markup_yields_none_pieces():
    facts = parse_bbb_profile(_addressless(_html()))

    assert facts["address"] is None
    assert facts["address_street"] is None
    assert facts["address_city"] is None


# ── first observation: baseline persisted, nothing emitted ──────────────────


def test_first_observation_persists_baseline_without_candidate(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)

    assert _parse(registry, account, _html()) == []

    stored = registry.get("avalara.com")
    assert stored.extra_data["bbb_address"] == OLD_ADDRESS
    assert stored.extra_data["bbb_address_observed_at"] == TODAY
    # Sparse upsert must NOT clobber unrelated extra_data keys (bbb_url is
    # what keeps plan() emitting tasks at all).
    assert stored.extra_data["bbb_url"] == BBB_URL


def test_address_absent_then_present_is_still_first_observation(tmp_path):
    """No address on either side must never compare None vs value."""
    registry = _registry(tmp_path)
    account = _account(registry)

    # Cycle 1: page renders no address — nothing stored, nothing emitted.
    assert _parse(registry, account, _addressless(_html())) == []
    assert "bbb_address" not in registry.get("avalara.com").extra_data

    # Cycle 2: address appears — first real observation, baseline only.
    assert _relocs(_parse(registry, account, _html())) == []
    assert registry.get("avalara.com").extra_data["bbb_address"] == OLD_ADDRESS


# ── address change: exactly one relocation ──────────────────────────────────


def test_address_change_emits_exactly_one_relocation(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    assert _parse(registry, account, _html()) == []  # baseline: Seattle

    cands = _parse(registry, account, _moved(_html()))

    relocs = _relocs(cands)
    assert len(relocs) == 1
    r = relocs[0]
    assert r.natural_key == f"reloc:avalara.com:{stable_id(NEW_ADDRESS, length=12)}"
    assert r.observed_at == TODAY
    assert r.title == f"BBB address {OLD_ADDRESS} → {NEW_ADDRESS}"
    assert NEW_ADDRESS in r.summary and OLD_ADDRESS in r.summary
    assert r.url == BBB_URL
    assert r.confidence == 0.7
    assert r.domain_override == "avalara.com"
    assert r.evidence_data["old_address"] == OLD_ADDRESS
    assert r.evidence_data["new_address"] == NEW_ADDRESS
    assert r.evidence_data["address_street"] == "1200 112th Ave NE"
    assert r.evidence_data["address_city"] == "Bellevue"
    assert r.evidence_data["address_state"] == "WA"
    assert r.evidence_data["address_zip"] == "98004"
    # Baseline advanced: the NEXT cycle diffs against Bellevue, not Seattle.
    assert registry.get("avalara.com").extra_data["bbb_address"] == NEW_ADDRESS


def test_repeated_moves_get_distinct_natural_keys(tmp_path):
    """A further move must hash to a new key, not reuse the previous one."""
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())
    (first,) = _relocs(_parse(registry, account, _moved(_html())))

    moved_again = _moved(_html()).replace(
        _LDJSON_ADDR_MOVED,
        '"address":{"@type":"PostalAddress","addressLocality":"Renton",'
        '"addressRegion":"WA","postalCode":"98057","addressCountry":"USA",'
        '"streetAddress":"2000 Lind Ave SW"}',
    ).replace(_HTML_STREET_MOVED, '<p class="bds-body" translate="no">2000 Lind Ave SW</p>').replace(
        _HTML_CITYZIP_MOVED,
        '<p class="bds-body" translate="no">Renton<!-- -->, <!-- -->WA'
        "<!-- --> <!-- -->98057</p>",
    )
    (second,) = _relocs(_parse(registry, account, moved_again))

    assert second.natural_key != first.natural_key
    assert second.natural_key == f"reloc:avalara.com:{stable_id('2000 Lind Ave SW, Renton, WA 98057', length=12)}"


# ── unchanged / missing markup: silent ───────────────────────────────────────


def test_unchanged_address_emits_nothing(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline Seattle

    assert _relocs(_parse(registry, account, _html())) == []
    assert registry.get("avalara.com").extra_data["bbb_address"] == OLD_ADDRESS


def test_missing_address_markup_never_erases_baseline_nor_emits(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline Seattle

    # Next cycle's page stops rendering the address: no candidate, and the
    # stored baseline survives for the cycle after.
    assert _relocs(_parse(registry, account, _addressless(_html()))) == []
    assert registry.get("avalara.com").extra_data["bbb_address"] == OLD_ADDRESS


# ── persistence: relocation survives normalize_batch + re-upsert ────────────


def test_relocation_persists_through_normalize_batch(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline Seattle
    (cand,) = _relocs(_parse(registry, account, _moved(_html())))

    # The real adapter key, pinned so the source column stays truthful.
    assert BbbProfileSource.key == "bbb_profile"
    assert "relocation" in BbbProfileSource.emits
    tax = Taxonomy.load()
    spec = tax.get("relocation")
    # score inputs pinned: operational type — positive, secondary catalyst.
    assert (spec.category, spec.catalyst, spec.polarity, spec.origin) == (
        "operational", "secondary", "positive", "internal",
    )
    assert spec.weight == 12 and spec.half_life_days == 120
    assert spec.play == "infrastructure_pitch"

    valid, rejected = normalize_batch(
        [cand],
        account=account,
        source=BbbProfileSource.key,
        taxonomy=tax,
        now=f"{TODAY}T00:00:00Z",
    )
    assert rejected == []
    assert len(valid) == 1
    sig = valid[0]
    assert sig.signal_type == "relocation"
    assert sig.category == "operational"
    assert sig.polarity == "positive"
    assert sig.source == "bbb_profile"
    assert sig.signal_id == make_signal_id(
        "avalara.com", "relocation", f"reloc:avalara.com:{stable_id(NEW_ADDRESS, length=12)}"
    )
    assert render_evidence(sig, account=account, today=date(2026, 9, 5)) == (
        f"BBB address {OLD_ADDRESS} → {NEW_ADDRESS} (today)"
    )


def test_relocation_row_is_idempotent_on_reupsert(tmp_path):
    """Same address change re-collected → same natural key → one row."""
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline Seattle
    (cand,) = _relocs(_parse(registry, account, _moved(_html())))

    tax = Taxonomy.load()

    def _normalized(now: str):
        valid, rejected = normalize_batch(
            [cand],
            account=account,
            source=BbbProfileSource.key,
            taxonomy=tax,
            now=now,
        )
        assert not rejected
        return valid

    db = Database(tmp_path / "reloc.db")
    try:
        store = SignalStore(db, taxonomy=tax)
        assert store.upsert_many(_normalized("2026-09-05T08:00:00Z")) == (1, 0)
        # Re-collected later the same day: identical candidate, later crawl.
        assert store.upsert_many(_normalized("2026-09-05T20:00:00Z")) == (0, 1)
        rows = db.query("SELECT * FROM signals WHERE domain = ?", (account.domain,))
        assert len(rows) == 1
        assert rows[0]["signal_type"] == "relocation"
        assert rows[0]["category"] == "operational"
        assert rows[0]["polarity"] == "positive"
        assert rows[0]["confidence"] == 0.7
    finally:
        db.close()

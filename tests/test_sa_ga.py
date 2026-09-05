"""Task 16: Software Advice + GetApp (Gartner network) marketplace adapters.

Fixture-built per the P3 spike (data/probe/P3_SOURCE_SPIKE_2026_09.md): both
sites serve server-rendered product pages whose JSON-LD carries the review
data — Software Advice embeds a ``SoftwareApplication/Product`` @graph with
per-review ``Review`` nodes (reviewBody + 0-5 reviewRating + "July 2026"
month-precision dates); GetApp embeds a ``SoftwareApplication`` with
aggregateRating plus positive/negativeNotes ItemLists (note text + author;
no per-note ratings or dates). No live fetches — every test parses the local
fixtures in tests/fixtures/marketplace/.

Persistence contract derived from code (not re-invented): the runner's
harvest_reviews dispatch ends in ``else: upsert_g2_reviews(...)`` for any new
``marketplace_*`` key, so harvest_reviews must return objects exposing the
exact attribute set upsert_g2_reviews reads (the CapterraReview shape).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from src.core.models import Account, Document
from src.signals.normalize import normalize_batch
from src.signals.taxonomy import Taxonomy

from src.sources.marketplace.sa_ga import (
    GartnerReview,
    MarketplaceGetAppSource,
    MarketplaceSoftwareAdviceSource,
    extract_jsonld,
    parse_ga_product,
    parse_sa_product,
    product_slug_from_url,
)

SA_URL = "https://www.softwareadvice.com/crm/claritysoft-profile/"
GA_URL = "https://www.getapp.com/customer-management-software/a/salesforce/"

SA_HTML = Path("tests/fixtures/marketplace/sa_product.html").read_text(encoding="utf-8")
GA_HTML = Path("tests/fixtures/marketplace/ga_product.html").read_text(encoding="utf-8")

# The exact attribute set upsert_g2_reviews (marketplace/collector.py) reads
# off every harvest_reviews object — this IS the runner else-branch contract.
UPSERT_FIELDS = {
    "review_id", "product_slug", "reviewer_name", "reviewer_title",
    "reviewer_company_size", "rating", "review_title", "review_body",
    "pros", "cons", "posted_at", "review_url", "verified_reviewer",
    "review_source", "nps_score", "helpful_votes",
}


def _sa_doc(body=SA_HTML):
    return Document(doc_id="d-sa", source="marketplace_softwareadvice", url=SA_URL,
                    body=body.encode("utf-8"))


def _ga_doc(body=GA_HTML):
    return Document(doc_id="d-ga", source="marketplace_getapp", url=GA_URL,
                    body=body.encode("utf-8"))


# ── extract_jsonld: fail-open ld+json block extraction ───────────────────────


def test_extract_jsonld_parses_all_wellformed_blocks():
    blocks = extract_jsonld(SA_HTML.encode("utf-8"))
    # breadcrumb + SoftwareApplication graph; the malformed block is skipped
    assert len(blocks) == 2
    types = {b.get("@type") for b in blocks}
    assert "BreadcrumbList" in types
    graph_entry = [b for b in blocks if "@graph" in b][0]
    assert graph_entry["@graph"][0]["name"] == "Claritysoft CRM"


def test_extract_jsonld_empty_and_garbage_inputs():
    assert extract_jsonld(b"") == []
    assert extract_jsonld(b"<html><body>no json-ld here</body></html>") == []
    assert extract_jsonld(b'<script type="application/ld+json">{oops</script>') == []


def test_extract_jsonld_getapp_blocks():
    blocks = extract_jsonld(GA_HTML.encode("utf-8"))
    assert len(blocks) == 2
    app = [b for b in blocks if b.get("@type") == "SoftwareApplication"][0]
    assert app["name"] == "Salesforce Sales Cloud"
    assert app["aggregateRating"]["ratingCount"] == 18807


# ── parse_sa_product: JSON-LD Review nodes → GartnerReview rows ─────────────


def test_parse_sa_product_count_and_provenance():
    reviews = parse_sa_product(SA_HTML, url=SA_URL)
    assert len(reviews) == 5  # per-review nodes only; aggregateRating is NOT a review
    assert all(isinstance(r, GartnerReview) for r in reviews)
    assert all(r.product_slug == "claritysoft-profile" for r in reviews)
    assert all(r.review_source == "softwareadvice" for r in reviews)


def test_sa_rating_passthrough_no_scale_conversion():
    """JSON-LD ratings are already 0-5: they pass through untouched.

    TrustRadius halves its 0-10 scale; SA/GA must NOT (spike: bestRating "5").
    """
    reviews = parse_sa_product(SA_HTML, url=SA_URL)
    assert [r.rating for r in reviews] == [5.0, 4.0, None, 5.0, 4.0]


def test_sa_missing_optional_fields_tolerated():
    """Robin's review has no rating, date, or employer — parsed, never invented."""
    robin = [r for r in parse_sa_product(SA_HTML, url=SA_URL) if r.reviewer_name == "Robin"][0]
    assert robin.rating is None
    assert robin.posted_at is None
    assert robin.reviewer_company_size is None
    assert robin.reviewer_title is None
    assert robin.review_body and "much bigger CRM" in robin.review_body


def test_sa_month_year_dates_normalize_to_ym():
    """'July 2026' → '2026-07': honest month precision, no fabricated day."""
    reviews = parse_sa_product(SA_HTML, url=SA_URL)
    by_name = {r.reviewer_name: r for r in reviews}
    assert by_name["Mandi"].posted_at == "2026-07"
    assert by_name["David"].posted_at == "2026-06"
    assert by_name["Heidi"].posted_at == "2017-01"
    # Full 'Month D, YYYY' dates normalize to full ISO days.
    assert by_name["Blake"].posted_at == "2026-01-15"


def test_sa_reviewer_fields_from_jsonld():
    reviews = parse_sa_product(SA_HTML, url=SA_URL)
    mandi = reviews[0]
    assert mandi.reviewer_name == "Mandi"
    assert mandi.reviewer_company_size == "2-10 employees"
    assert mandi.verified_reviewer is False  # no verification markup in JSON-LD


def test_sa_review_id_stable_across_parses():
    a = parse_sa_product(SA_HTML, url=SA_URL)
    b = parse_sa_product(SA_HTML, url=SA_URL)
    assert [r.review_id for r in a] == [r.review_id for r in b]
    assert all(len(r.review_id) == 16 for r in a)


def test_sa_review_id_differs_for_different_content():
    """A changed review body must change the id (reviewer+date alone can
    collide: SA reviewer names are first names, dates are month-precision)."""
    robin_a = [r for r in parse_sa_product(SA_HTML, url=SA_URL) if r.reviewer_name == "Robin"][0]
    edited = SA_HTML.replace("much bigger CRM and our reps", "much smaller CRM and our reps")
    robin_b = [r for r in parse_sa_product(edited, url=SA_URL) if r.reviewer_name == "Robin"][0]
    assert robin_b.review_id != robin_a.review_id
    # ...while the other reviews keep their ids.
    mandi_a = [r for r in parse_sa_product(SA_HTML, url=SA_URL) if r.reviewer_name == "Mandi"][0]
    mandi_b = [r for r in parse_sa_product(edited, url=SA_URL) if r.reviewer_name == "Mandi"][0]
    assert mandi_a.review_id == mandi_b.review_id


# ── parse_ga_product: positive/negativeNotes → GartnerReview rows ───────────


def test_parse_ga_product_count_and_provenance():
    reviews = parse_ga_product(GA_HTML, url=GA_URL)
    # 2 positiveNotes + 1 negativeNotes item; aggregateRating is NOT a review.
    assert len(reviews) == 3
    assert all(isinstance(r, GartnerReview) for r in reviews)
    assert all(r.product_slug == "salesforce" for r in reviews)
    assert all(r.review_source == "getapp" for r in reviews)


def test_ga_note_items_carry_polarity_and_no_invented_fields():
    """Per-note ratings/dates are NOT present in the live JSON-LD (spike) —
    they parse as None, never guessed. Note text lands in pros/cons + body."""
    reviews = parse_ga_product(GA_HTML, url=GA_URL)
    positive = reviews[0]
    assert positive.reviewer_name == "khyan adair"
    assert positive.pros and "very capable platform" in positive.pros[0]
    assert positive.review_body == positive.pros[0]
    assert positive.cons == []
    negative = reviews[-1]
    assert negative.cons and "Pricing can climb quickly" in negative.cons[0]
    assert negative.review_body == negative.cons[0]
    assert negative.pros == []
    for r in reviews:
        assert r.rating is None
        assert r.posted_at is None


def test_ga_missing_author_tolerated():
    """The negativeNotes item has no author — reviewer_name stays None."""
    negative = parse_ga_product(GA_HTML, url=GA_URL)[-1]
    assert negative.reviewer_name is None


def test_ga_review_id_stable_and_content_sensitive():
    a = parse_ga_product(GA_HTML, url=GA_URL)
    b = parse_ga_product(GA_HTML, url=GA_URL)
    assert [r.review_id for r in a] == [r.review_id for r in b]
    edited = GA_HTML.replace("custom workflows", "custom workstreams")
    c = parse_ga_product(edited, url=GA_URL)
    assert c[0].review_id != a[0].review_id
    assert c[1].review_id == a[1].review_id


def test_product_slug_from_url():
    assert product_slug_from_url(SA_URL) == "claritysoft-profile"
    assert product_slug_from_url(GA_URL) == "salesforce"
    assert product_slug_from_url("https://www.getapp.com/") == ""


# ── adapters: plan / parse / harvest_reviews ─────────────────────────────────


def test_adapter_identity_and_tier():
    sa = MarketplaceSoftwareAdviceSource()
    ga = MarketplaceGetAppSource()
    assert sa.key == "marketplace_softwareadvice"
    assert ga.key == "marketplace_getapp"
    assert sa.tier == "http" and ga.tier == "http"  # browser-free, curl_cffi fetcher
    assert sa.cadence_hours == 168 and ga.cadence_hours == 168


def test_sa_plan_uses_extra_data_url_and_slug_meta():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    (task,) = sa.plan(acct, None)
    assert task.source == "marketplace_softwareadvice"
    assert task.url == SA_URL
    assert task.domain == "claritysoft.com"
    assert task.meta["kind"] == "reviews"
    assert task.meta["product_slug"] == "claritysoft-profile"
    assert task.meta["page"] == 1
    assert task.meta["review_lookback_days"] == 90


def test_sa_plan_without_url_plans_nothing():
    sa = MarketplaceSoftwareAdviceSource()
    assert sa.plan(Account(domain="claritysoft.com"), None) == []


def test_ga_plan_uses_extra_data_url_and_slug_meta():
    ga = MarketplaceGetAppSource()
    acct = Account(domain="salesforce.com", extra_data={"getapp_url": GA_URL})
    (task,) = ga.plan(acct, None)
    assert task.source == "marketplace_getapp"
    assert task.url == GA_URL
    assert task.meta["product_slug"] == "salesforce"


def test_ga_plan_without_url_plans_nothing():
    ga = MarketplaceGetAppSource()
    assert ga.plan(Account(domain="salesforce.com"), None) == []


def test_sa_harvest_reviews_returns_review_objects():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    revs = sa.harvest_reviews(_sa_doc(), acct, {"product_slug": "claritysoft-profile"})
    assert len(revs) == 5
    assert all(isinstance(r, GartnerReview) for r in revs)
    assert all(r.product_slug == "claritysoft-profile" for r in revs)


def test_harvest_slug_falls_back_to_account_extra_data_url():
    """No product_slug in meta: the adapter re-derives it from the account URL."""
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    revs = sa.harvest_reviews(_sa_doc(), acct, {})
    assert all(r.product_slug == "claritysoft-profile" for r in revs)
    ga = MarketplaceGetAppSource()
    gacct = Account(domain="salesforce.com", extra_data={"getapp_url": GA_URL})
    grevs = ga.harvest_reviews(_ga_doc(), gacct, {})
    assert all(r.product_slug == "salesforce" for r in grevs)


def test_harvest_reviews_objects_match_upsert_contract():
    """The runner's else-branch upsert reads these attributes bare — a missing
    one is an AttributeError mid-cycle (the appstore-dict precedent)."""
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    revs = sa.harvest_reviews(_sa_doc(), acct, {"product_slug": "claritysoft-profile"})
    got = {f.name for f in dataclasses.fields(GartnerReview)}
    assert got == UPSERT_FIELDS
    for r in revs:
        for name in UPSERT_FIELDS:
            assert hasattr(r, name)


def test_sa_parse_emits_intent_2nd_marketplace_candidates():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    meta = {"today": "2026-08-28", "product_slug": "claritysoft-profile"}
    cands = sa.parse(_sa_doc(), acct, meta)
    # 5 reviews parsed; Blake's full-ISO 2026-01-15 date is beyond the default
    # 90-day lookback and is skipped. Month-precision dates ("2017-01") are
    # unparseable for the filter, so those reviews pass (family tolerance).
    assert len(cands) == 4
    assert all(c.signal_type == "intent_2nd_marketplace" for c in cands)
    assert all(c.natural_key.startswith("sarev:claritysoft-profile:") for c in cands)
    assert all(c.observed_at == "2026-08-28" for c in cands)
    assert all(c.evidence_data["product_slug"] == "claritysoft-profile" for c in cands)
    assert all(c.evidence_data["review_source"] == "softwareadvice" for c in cands)
    assert all(c.confidence > 0 for c in cands)


def test_sa_parse_wide_lookback_keeps_full_iso_old_dates():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    meta = {"today": "2026-08-28", "product_slug": "claritysoft-profile",
            "review_lookback_days": 400}
    cands = sa.parse(_sa_doc(), acct, meta)
    assert len(cands) == 5
    blake = [c for c in cands if "Blake" in (c.title or "")][0]
    assert blake.observed_at == "2026-01-15"


def test_sa_parse_without_today_emits_nothing():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    assert sa.parse(_sa_doc(), acct, {"product_slug": "claritysoft-profile"}) == []


def test_sa_parse_empty_body_emits_nothing():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    assert sa.parse(_sa_doc(body="<html></html>"), acct, {"today": "2026-08-28"}) == []


def test_ga_parse_emits_candidates_with_safe_title_fallback():
    ga = MarketplaceGetAppSource()
    acct = Account(domain="salesforce.com", extra_data={"getapp_url": GA_URL})
    meta = {"today": "2026-08-28", "product_slug": "salesforce"}
    cands = ga.parse(_ga_doc(), acct, meta)
    assert len(cands) == 3
    assert all(c.signal_type == "intent_2nd_marketplace" for c in cands)
    assert all(c.natural_key.startswith("garev:salesforce:") for c in cands)
    # The author-less negative note must not render as "Review by None".
    assert "Review by None" not in [c.title for c in cands]
    untitled = [c for c in cands if c.title == "Marketplace review"]
    assert len(untitled) == 1


# ── persistence smoke: runner else-branch upsert + normalize_batch ───────────


def test_sa_harvest_persists_through_upsert_g2_reviews(tmp_path):
    from src.core.db import Database
    from src.sources.marketplace.collector import upsert_g2_reviews

    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    revs = sa.harvest_reviews(_sa_doc(), acct, {"product_slug": "claritysoft-profile"})
    db = Database(tmp_path / "sa.db")
    new, updated = upsert_g2_reviews(db, revs, now="2026-09-05T00:00:00+00:00",
                                     raw_ref="d-sa")
    assert (new, updated) == (5, 0)
    row = db.one("SELECT * FROM g2_reviews WHERE reviewer_name=?", ("Mandi",))
    assert row["rating"] == 5.0
    assert row["posted_at"] == "2026-07"
    assert row["review_source"] == "softwareadvice"
    assert row["review_body"] and "Claritysoft daily" in row["review_body"]
    assert row["raw_ref"] == "d-sa"
    # Re-upserting the same cycle is idempotent on review_id.
    new, updated = upsert_g2_reviews(db, revs, now="2026-09-05T00:00:00+00:00")
    assert (new, updated) == (0, 5)


def test_ga_harvest_persists_through_upsert_g2_reviews(tmp_path):
    from src.core.db import Database
    from src.sources.marketplace.collector import upsert_g2_reviews

    ga = MarketplaceGetAppSource()
    acct = Account(domain="salesforce.com", extra_data={"getapp_url": GA_URL})
    revs = ga.harvest_reviews(_ga_doc(), acct, {"product_slug": "salesforce"})
    db = Database(tmp_path / "ga.db")
    new, updated = upsert_g2_reviews(db, revs, now="2026-09-05T00:00:00+00:00",
                                     raw_ref="d-ga")
    assert (new, updated) == (3, 0)
    row = db.one("SELECT * FROM g2_reviews WHERE reviewer_name=?", ("khyan adair",))
    assert row["review_source"] == "getapp"
    assert row["rating"] is None
    assert "very capable platform" in row["review_body"]
    assert "very capable platform" in json.loads(row["pros"])[0]
    orphan = db.one("SELECT * FROM g2_reviews WHERE reviewer_name IS NULL")
    assert "Pricing can climb quickly" in orphan["review_body"]
    assert "Pricing can climb quickly" in json.loads(orphan["cons"])[0]


def test_sa_candidates_normalize_with_real_adapter_key():
    sa = MarketplaceSoftwareAdviceSource()
    acct = Account(domain="claritysoft.com", extra_data={"sa_url": SA_URL})
    cands = sa.parse(_sa_doc(), acct, {"today": "2026-08-28",
                                       "product_slug": "claritysoft-profile"})
    valid, rejected = normalize_batch(
        cands, account=acct, source=sa.key, taxonomy=Taxonomy.load(),
        now="2026-08-28T00:00:00Z",
    )
    assert rejected == []
    assert len(valid) == 4
    assert all(s.source == "marketplace_softwareadvice" for s in valid)
    assert all(s.signal_type == "intent_2nd_marketplace" for s in valid)


def test_ga_candidates_normalize_with_real_adapter_key():
    ga = MarketplaceGetAppSource()
    acct = Account(domain="salesforce.com", extra_data={"getapp_url": GA_URL})
    cands = ga.parse(_ga_doc(), acct, {"today": "2026-08-28",
                                       "product_slug": "salesforce"})
    valid, rejected = normalize_batch(
        cands, account=acct, source=ga.key, taxonomy=Taxonomy.load(),
        now="2026-08-28T00:00:00Z",
    )
    assert rejected == []
    assert len(valid) == 3
    assert all(s.source == "marketplace_getapp" for s in valid)

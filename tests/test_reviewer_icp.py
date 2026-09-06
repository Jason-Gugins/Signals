"""Reviewer-ICP join (plan Task 13): persisted g2_reviews rows carry
``reviewer_title`` / ``reviewer_company_size`` that nothing reads back.

Pure matcher: ``reviewer_icp_matches()`` in ``src/sources/marketplace/trend.py``
casefold-matches reviewer titles against the icp.yaml ``reviewer_titles`` list
(substring BOTH ways), with an optional company-size band check parsed from
tokens like "51-200" (midpoint when both bounds numeric).

Runner wiring: in the review-harvest region, after the upsert dispatch, for
marketplace_* adapters whose harvested reviews carry reviewer fields, matching
reviews emit the family's EXISTING ``intent_2nd_marketplace`` type with
``icp_match`` evidence and a forever-deduping ``icprev:{source}:{review_id}``
natural key — the no-migration mechanism (g2_reviews has no JSON/evidence
column). The marketplace adapters ship disabled; this lands dark-but-wired.
"""

from __future__ import annotations

import json

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.runner import CollectorRunner
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.marketplace.g2 import G2Review
from src.sources.marketplace.trend import reviewer_icp_matches, size_midpoint

DOMAIN = "acme.com"
TARGETS = ["VP Sales", "Head of Revenue"]


# ── Pure matcher (no clock, no I/O) ─────────────────────────────────────────


def test_matcher_title_casefold_both_ways():
    # title contains the target ("VP Sales" inside a longer title).
    assert reviewer_icp_matches("VP Sales & Marketing", None, TARGETS, None) is True
    # target contains the title, casefolded ("Head of Revenue" inside a
    # lowercased longer title).
    assert reviewer_icp_matches("head of revenue operations", None, TARGETS, None) is True
    assert reviewer_icp_matches("vp sales", None, ["VP Sales"], None) is True


def test_matcher_non_matching_titles():
    assert reviewer_icp_matches("CTO", None, TARGETS, None) is False
    assert reviewer_icp_matches("Office Manager", None, TARGETS, None) is False
    # Empty/None title and empty target list never match.
    assert reviewer_icp_matches(None, None, TARGETS, None) is False
    assert reviewer_icp_matches("   ", None, TARGETS, None) is False
    assert reviewer_icp_matches("VP Sales", None, [], None) is False


def test_matcher_size_band_parse_and_filter():
    # "51-200" -> midpoint 125, inside (50, 200) -> match.
    assert size_midpoint("51-200") == 125
    assert size_midpoint("1,001-5,000") == 3000
    assert size_midpoint("200") == 200
    # One-sided or non-numeric tokens have no midpoint.
    assert size_midpoint("10,000+") is None
    assert size_midpoint("Enterprise") is None
    assert size_midpoint(None) is None
    assert size_midpoint("") is None
    # Band given and satisfied -> still a match.
    assert reviewer_icp_matches("VP Sales", "51-200", TARGETS, (50, 200)) is True
    # Band given but midpoint outside -> no match.
    assert reviewer_icp_matches("VP Sales", "51-200", TARGETS, (1000, 5000)) is False
    # Band given but size missing/unparseable -> conservative no-match.
    assert reviewer_icp_matches("VP Sales", None, TARGETS, (50, 200)) is False
    assert reviewer_icp_matches("VP Sales", "Enterprise", TARGETS, (50, 200)) is False


def test_matcher_none_tolerance():
    assert reviewer_icp_matches(None, None, TARGETS, None) is False
    assert reviewer_icp_matches("VP Sales", None, TARGETS, None) is True  # no band -> title decides
    assert reviewer_icp_matches("VP Sales", "51-200", TARGETS, None) is True


# ── Taxonomy persistence pin (house rule after tech_churn) ─────────────────


def test_icp_reviewer_candidate_survives_normalize_batch():
    tax = Taxonomy.load()
    account = Account(domain=DOMAIN, name="Acme")
    cand = SignalCandidate(
        signal_type="intent_2nd_marketplace",
        observed_at="2026-09-04",
        natural_key=f"icprev:marketplace_g2:rv-1",
        title="ICP reviewer: VP Sales & Marketing",
        confidence=0.7,
        evidence_data={
            "reviewer_title": "VP Sales & Marketing",
            "reviewer_company_size": "51-200",
            "icp_match": True,
            "review_id": "rv-1",
        },
    )
    valid, rej = normalize_batch(
        [cand], account=account, source="marketplace_g2", taxonomy=tax, now="2026-09-04"
    )
    assert not rej
    sig = valid[0]
    assert sig.signal_type == "intent_2nd_marketplace"
    assert sig.category == "intent"
    assert sig.degree == 2
    assert sig.domain == DOMAIN
    assert sig.source == "marketplace_g2"
    assert sig.evidence_data["icp_match"] is True


# ── Runner wiring: marketplace harvests emit ICP-matched reviewer signals ───


class _FakeFetch:
    def __init__(self, by_url: dict):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        body = self.by_url[task.url]
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=bytes(body), status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _G2Stub(SourceAdapter):
    """marketplace_g2-shaped adapter: harvest_reviews yields real G2Review
    objects so the runner's upsert dispatch AND the reviewer-ICP join both
    run without real HTTP."""

    key = "marketplace_g2"
    tier = "http"
    cadence_hours = 24

    def __init__(self, reviews: list):
        self._reviews = reviews

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url=f"https://www.g2.com/products/{account.g2_slug}/reviews",
                domain=account.domain,
                meta={"kind": "reviews", "product_slug": account.g2_slug, "page": 1},
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_reviews(self, doc, account, task_meta):
        return list(self._reviews)


def _reviews():
    return [
        G2Review(
            review_id="rv-icp-1",
            product_slug="acme",
            reviewer_name="Jane Doe",
            reviewer_title="VP Sales & Marketing",  # matches icp.yaml "VP Sales"
            reviewer_company_size="51-200",
            rating=5.0,
            posted_at="2026-08-20",
        ),
        G2Review(
            review_id="rv-icp-2",
            product_slug="acme",
            reviewer_name="Bob Roe",
            reviewer_title="Office Manager",  # no ICP title match
            reviewer_company_size="1,001-5,000",
            rating=3.0,
            posted_at="2026-08-21",
        ),
    ]


def _harness(tmp_path):
    db = Database(tmp_path / "s.db")
    tax = Taxonomy.load()
    store = RawStore(db, tmp_path / "raw")
    cfg = Config()
    cfg.http.max_workers = 2
    cfg.http.respect_robots = False
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetcher = _FakeFetch(
        {"https://www.g2.com/products/acme/reviews": b"<html><body>reviews</body></html>"}
    )
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher, SignalStore(db, tax), tax, ctx
    )
    return runner, db, ctx


def test_runner_emits_icp_matched_reviewer_signal(tmp_path):
    runner, db, ctx = _harness(tmp_path)
    account = Account(domain=DOMAIN, name="Acme", g2_slug="acme")
    adapter = _G2Stub(_reviews())
    stats1 = runner.run([adapter], [account], force=True)
    # The harvest itself upserted both reviews into g2_reviews ...
    assert db.one("SELECT COUNT(*) AS n FROM g2_reviews")["n"] == 2
    # ... and the ICP join persisted exactly ONE matched-reviewer signal.
    rows = db.query("SELECT * FROM signals WHERE signal_type='intent_2nd_marketplace'")
    assert len(rows) == 1
    row = rows[0]
    assert row["domain"] == DOMAIN
    assert row["source"] == "marketplace_g2"
    assert row["signal_id"] == make_signal_id(
        DOMAIN, "intent_2nd_marketplace", "icprev:marketplace_g2:rv-icp-1"
    )
    ev = json.loads(row["evidence_data"])
    assert ev["icp_match"] is True
    assert ev["reviewer_title"] == "VP Sales & Marketing"
    assert ev["reviewer_company_size"] == "51-200"
    assert ev["review_id"] == "rv-icp-1"
    assert stats1.signals_new == 1
    # Re-run: the icprev natural key dedupes FOREVER -> zero new signals.
    stats2 = runner.run([adapter], [account], force=True)
    assert stats2.signals_new == 0
    assert db.one("SELECT COUNT(*) AS n FROM signals WHERE signal_type='intent_2nd_marketplace'")["n"] == 1
    ctx.__exit__(None, None, None)


def test_runner_join_is_gated_to_marketplace_adapters(tmp_path):
    runner, db, ctx = _harness(tmp_path)
    account = Account(domain=DOMAIN, name="Acme", g2_slug="acme")
    off_family = _G2Stub(_reviews())
    off_family.key = "appstore_reviews"  # review-shaped but NOT marketplace_*
    stats = runner.run([off_family], [account], force=True)
    assert db.query("SELECT * FROM signals WHERE signal_type='intent_2nd_marketplace'") == []
    assert stats.signals_new == 0
    ctx.__exit__(None, None, None)


def test_runner_silent_when_icp_has_no_reviewer_titles(tmp_path, monkeypatch):
    runner, db, ctx = _harness(tmp_path)
    account = Account(domain=DOMAIN, name="Acme", g2_slug="acme")
    adapter = _G2Stub(_reviews())
    # Fail-open contract: an ICP doc without reviewer_titles emits nothing.
    original = Config.load_yaml

    def _no_reviewer_titles(self, name):
        if name == "icp":
            return {}
        return original(self, name)

    monkeypatch.setattr(Config, "load_yaml", _no_reviewer_titles)
    stats = runner.run([adapter], [account], force=True)
    assert db.query("SELECT * FROM signals WHERE signal_type='intent_2nd_marketplace'") == []
    assert stats.signals_new == 0
    ctx.__exit__(None, None, None)

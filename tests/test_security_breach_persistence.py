"""Regression: security_breach must persist through the real news path.

T9 of the keyless-clay-exa-clones plan: the type only becomes real once a
canned breach headline classified through the actual NewsRule path survives
normalize_batch and lands in the signals table exactly once. A re-upsert of
the same article (same natural key) must not duplicate the row
(tech_churn / federal_contract_award precedent).
"""

from __future__ import annotations

from datetime import date

from src.core.db import Database
from src.core.models import Account
from src.core.textutil import sha256_hex
from src.signals.evidence import render_evidence
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.news.classify import _canon_link, classify_news
from src.sources.news.feeds import NewsItem

TODAY = date(2026, 9, 5)
ACCOUNT = Account(domain="acme.com", name="Acme")
LINK = "https://www.bleepingcomputer.com/news/security/acme-discloses-data-breach/"
ITEM = NewsItem(
    title="Acme discloses a data breach exposing customer records",
    link=LINK,
    published="2026-09-03",
    summary="",
    source_name="BleepingComputer",
)


def _classify():
    cand = classify_news(ITEM, ACCOUNT, today=TODAY)
    assert cand is not None, "security_breach rule did not fire on the canned headline"
    return cand


def test_security_breach_classifies_through_real_news_path():
    cand = _classify()
    assert cand.signal_type == "security_breach"
    # House news natural-key scheme: first 16 hex chars of the sha256 of the
    # canonical link — the same mechanism every news-classified type uses.
    assert cand.natural_key == sha256_hex(_canon_link(LINK))[:16]
    assert cand.title == ITEM.title
    assert cand.url == LINK


def test_security_breach_persists_through_normalize_batch():
    tax = Taxonomy.load()
    assert "security_breach" in tax._types, (
        "security_breach missing from signals.yaml — breach candidates are rejected "
        "as unknown and never persist (tech_churn precedent)"
    )
    spec = tax.get("security_breach")
    # score inputs pinned: risk/urgency type — negative, secondary catalyst
    assert (spec.category, spec.catalyst, spec.polarity, spec.origin) == (
        "negative", "secondary", "negative", "internal",
    )
    assert spec.weight == 20 and spec.half_life_days == 45
    assert spec.play == "trust_rebuild_pitch"

    valid, rejected = normalize_batch(
        [_classify()],
        account=ACCOUNT,
        source="google_news",
        taxonomy=tax,
        now="2026-09-05T00:00:00Z",
    )
    assert not rejected
    assert [s.signal_type for s in valid] == ["security_breach"]
    sig = valid[0]
    assert sig.signal_id == make_signal_id(ACCOUNT.domain, "security_breach", _classify().natural_key)
    assert sig.domain == "acme.com"
    assert sig.source == "google_news"
    assert sig.category == "negative"
    assert sig.catalyst == "secondary"
    assert sig.polarity == "negative"
    assert sig.confidence == 0.65
    rendered = render_evidence(sig, account=ACCOUNT, today=TODAY)
    assert rendered == (
        "Security breach reported: Acme discloses a data breach exposing "
        "customer records (2 days ago)"
    )


def test_security_breach_row_is_idempotent_on_reupsert(tmp_path):
    """Same article fetched twice → same natural key → one row, no duplicate."""
    tax = Taxonomy.load()

    def _normalized(now: str):
        valid, rejected = normalize_batch(
            [_classify()],
            account=ACCOUNT,
            source="google_news",
            taxonomy=tax,
            now=now,
        )
        assert not rejected
        return valid

    db = Database(tmp_path / "breach.db")
    try:
        store = SignalStore(db, taxonomy=tax)
        assert store.upsert_many(_normalized("2026-09-05T08:00:00Z")) == (1, 0)
        # Re-collected later the same day: identical candidate, later crawl.
        assert store.upsert_many(_normalized("2026-09-05T20:00:00Z")) == (0, 1)
        rows = db.query("SELECT * FROM signals WHERE domain = ?", (ACCOUNT.domain,))
        assert len(rows) == 1
        assert rows[0]["signal_type"] == "security_breach"
        assert rows[0]["category"] == "negative"
        assert rows[0]["polarity"] == "negative"
        assert rows[0]["confidence"] == 0.65
    finally:
        db.close()

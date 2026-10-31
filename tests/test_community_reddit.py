"""Task 14 (D1) — Reddit Atom brand-search parser + adapter tests.

Replaces the retired old.reddit HTML stub tests (tests/test_reddit_parse.py):
the live-verified surface is www.reddit.com/search.rss (Atom), parsed with
feedparser against a synthetic fixture in Reddit's real structure
(tests/fixtures/community/reddit_search.rss). Network is blocked by the
autouse guard; everything here is offline and pure.
"""

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.community.reddit import parse_reddit_atom, reddit_search_url, reddit_to_candidates

FIXTURE = Path("tests/fixtures/community/reddit_search.rss").read_bytes()


def test_url_quoted_brand_search():
    url = reddit_search_url("Acme Corp")
    assert url.startswith("https://www.reddit.com/search.rss?")
    assert "%22acme%20corp%22" in url and "t=week" in url and "sort=new" in url
    sub = reddit_search_url("Acme", subreddit="saas")
    assert "/r/saas/search.rss" in sub and "restrict_sr=on" in sub


def test_parse_atom_entries():
    hits = parse_reddit_atom(FIXTURE)
    assert hits and hits[0]["title"] == "Acme Corp raised a Series B"
    assert hits[0]["id"].startswith("t3_")
    assert hits[0]["url"].startswith("https://www.reddit.com/")
    assert parse_reddit_atom(b"") == []


def test_candidates_intent_and_launch():
    acct = Account(domain="acme.com", name="Acme Corp")
    cands = reddit_to_candidates(parse_reddit_atom(FIXTURE), acct, today=date(2026, 10, 30))
    intents = [c for c in cands if c.signal_type == "intent_3rd_topic"]
    assert intents and intents[0].natural_key.startswith("redd:t3_")
    assert intents[0].confidence == 0.5
    assert all(c.evidence_data.get("subreddit") for c in intents)
    launches = [c for c in cands if c.signal_type == "product_launch"]
    assert launches and launches[0].natural_key.startswith("reddlaunch:t3_")


def test_body_mention_counts():
    acct = Account(domain="acme.com", name="Acme Corp")
    hits = [{"id": "t3_x", "title": "Anyone tried this?", "url": "https://www.reddit.com/r/sales/1",
             "created_at": "2026-10-29T10:00:00+00:00", "subreddit": "sales",
             "body": "we evaluated acme corp last quarter"}]
    cands = reddit_to_candidates(hits, acct, today=date(2026, 10, 30))
    assert [c.signal_type for c in cands] == ["intent_3rd_topic"]


def test_adapter_registered_plan_parse_roundtrip():
    """Adapter contract (re-homed from the retired stub tests): registered key,
    plan builds the quoted-brand search (+ capped subreddit scopes), parse()
    turns Atom bytes into candidates, empty body degrades to []."""
    from src.core.models import Document
    from src.sources.registry import SOURCES

    acct = Account(domain="acme.com", name="Acme Corp")
    today = date(2026, 10, 30)
    assert "community_reddit" in SOURCES
    cls = SOURCES["community_reddit"]
    assert cls.key == "community_reddit"
    assert cls.tier == "http"
    assert cls.cadence_hours == 72
    adapter = cls()
    # no extra_data subreddits -> one site-wide quoted-brand search task
    tasks = adapter.plan(acct, cursor=None)
    assert len(tasks) == 1
    assert tasks[0].url == reddit_search_url("Acme Corp")
    assert tasks[0].domain == "acme.com"
    # account-chosen subreddits: capped at 3, appended after the site-wide task
    acct2 = Account(domain="acme.com", name="Acme Corp",
                    extra_data={"reddit_subreddits": ["saas", "sales", "startups", "sysadmin"]})
    tasks2 = adapter.plan(acct2, cursor=None)
    assert [t.meta.get("subreddit") for t in tasks2] == [None, "saas", "sales", "startups"]
    assert tasks2[1].url == reddit_search_url("Acme Corp", subreddit="saas")
    # parse roundtrip: Atom bytes in, candidates out; empty body degrades to []
    doc = Document(doc_id="r1", source="community_reddit", url=tasks[0].url, body=FIXTURE)
    cands = adapter.parse(doc, acct, {"today": today.isoformat()})
    assert {c.signal_type for c in cands} == {"intent_3rd_topic", "product_launch"}
    assert any(c.natural_key == "redd:t3_1abcd2" for c in cands)
    assert any(c.natural_key == "reddlaunch:t3_4pqrs7" for c in cands)
    assert adapter.parse(Document(doc_id="r2", source="community_reddit", body=b""),
                         acct, {"today": today.isoformat()}) == []


# --- Live-capture fixture (Task 16, drift fixes) ----------------------------
# tests/fixtures/community/reddit_search_live.rss is a REAL capture: probed
# 2026-10-31 via collect --source community_reddit --force --domain
# brooklinen.com (HTTP 200, application/atom+xml) through the scoped
# /search.rss robots override — see tests/fixtures/community/REDDIT_LIVE.md.
# Its proven pinned: the live feed mixes t5_ SUBREDDIT entries with t3_ posts,
# which the synthetic fixture never had.
LIVE = Path("tests/fixtures/community/reddit_search_live.rss").read_bytes()


def test_live_fixture_posts_only():
    """t5_ subreddit rows (e.g. r/Bedding 'A subreddit devoted to bedding',
    r/malelivingspace — descriptions that merely mention the brand) must never
    become candidates; only t3_ posts do."""
    hits = parse_reddit_atom(LIVE)
    ids = {h["id"] for h in hits}
    assert any(i.startswith("t5_") for i in ids)  # the noise class is really present
    acct = Account(domain="brooklinen.com", name="Brooklinen")
    cands = reddit_to_candidates(hits, acct, today=date(2026, 10, 30))
    assert cands, "the t3_ posts discussing Brooklinen must still emit"
    assert all(c.natural_key.startswith("redd:t3_") for c in cands)


def test_live_fixture_subreddit_extracted_from_category():
    hits = parse_reddit_atom(LIVE)
    subs = {h["subreddit"] for h in hits if h["id"].startswith("t3_")}
    assert "BuyItForLife" in subs  # <category term="BuyItForLife" label="r/BuyItForLife"/>


def test_t5_subreddit_entries_yield_nothing():
    acct = Account(domain="acme.com", name="Acme")
    hits = [{"id": "t5_2v70p", "title": "acme", "url": "https://www.reddit.com/r/acme/",
             "created_at": "2011-01-01T00:00:00+00:00", "subreddit": None,
             "author": "/u/x", "body": "a subreddit devoted to acme"}]
    assert reddit_to_candidates(hits, acct, today=date(2026, 10, 30)) == []

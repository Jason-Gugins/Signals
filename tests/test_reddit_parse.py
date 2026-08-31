"""Task 7 — Reddit stub parser + adapter tests (synthetic fixture only).

Live fetching is blocked per P2 spike (www.reddit 403 block page + old.reddit
login wall): everything here runs against the committed synthetic fixture.
"""

from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.sources.community.reddit import RedditSource, parse_reddit_posts, reddit_to_candidates, reddit_url
from src.sources.registry import SOURCES

FIXTURE = Path("tests/fixtures/community/reddit_post_list.html")
ACCT = Account(domain="acme.com", name="Acme")
TODAY = date(2026, 8, 31)
SYNTHETIC_HEADER = "# synthetic — live capture blocked (P2 spike 2026-08-31, see data/probe/P2_SOURCE_SPIKE.md)"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_fixture_has_synthetic_header():
    assert SYNTHETIC_HEADER in _fixture_html()


def test_parse_reddit_posts_count_and_fields():
    posts = parse_reddit_posts(_fixture_html())
    assert len(posts) == 3
    ids = {p["id"] for p in posts}
    assert ids == {"t3_1abcd2", "t3_2wxyz9", "t3_3mnop4"}
    by_id = {p["id"]: p for p in posts}
    p1 = by_id["t3_1abcd2"]
    assert p1["title"] == "Anyone tried the new Acme forecasting tool? Reps on my team swear by it"
    assert p1["url"] == "https://news.example.com/acme-forecasting-tool-draws-praise-from-reps"
    assert p1["score"] == 42
    assert p1["created_at"] == "2026-08-24T14:30:00+00:00"
    assert p1["subreddit"] == "sales"
    assert p1["author"] == "buyer_bob"
    p2 = by_id["t3_2wxyz9"]
    assert p2["score"] == 7
    p3 = by_id["t3_3mnop4"]
    assert p3["score"] == 105
    assert p3["url"].startswith("https://old.reddit.com/r/sales/comments/")
    # empty / garbage input degrades to []
    assert parse_reddit_posts("") == []
    assert parse_reddit_posts("<html><body><p>login wall</p></body></html>") == []


def test_candidates_match_account_and_dedupe_on_post_id():
    posts = parse_reddit_posts(_fixture_html())
    cands = reddit_to_candidates(posts, ACCT, today=TODAY)
    # Only Acme-titled posts pass the name filter (post 3 mentions no company)
    intent_keys = [c.natural_key for c in cands if c.signal_type == "intent_3rd_topic"]
    assert intent_keys == ["redd:t3_1abcd2", "redd:t3_2wxyz9"]
    # dedupe on post id: feeding the same post twice adds no duplicate key
    doubled = reddit_to_candidates(posts + posts, ACCT, today=TODAY)
    assert [c.natural_key for c in doubled if c.signal_type == "intent_3rd_topic"] == intent_keys
    # confidence mirrors hn.py scoring (42 points -> 0.4 + 0.0; no, 42//50=0 -> 0.4)
    c1 = next(c for c in cands if c.natural_key == "redd:t3_1abcd2")
    assert abs(c1.confidence - 0.4) < 1e-9
    assert c1.evidence_data["score"] == 42
    assert c1.observed_at == "2026-08-24"
    assert c1.url == "https://news.example.com/acme-forecasting-tool-draws-praise-from-reps"


def test_launch_detection_on_own_domain_title():
    posts = parse_reddit_posts(_fixture_html())
    cands = reddit_to_candidates(posts, ACCT, today=TODAY)
    launches = [c for c in cands if c.signal_type == "product_launch"]
    # post 2: launch verbs ("launching", "announcing") + acme.com link on
    # the account's own domain -> product_launch; post 1 has launch-adjacent
    # phrasing but no launch verb in the title -> intent only.
    assert [c.natural_key for c in launches] == ["reddlaunch:t3_2wxyz9"]
    assert launches[0].confidence == 0.6
    assert launches[0].url == "https://acme.com/blog/introducing-acme-pipeline-ai"
    # a launch-verb title on a foreign domain must NOT produce product_launch
    foreign = [
        {"id": "t3_foreign", "title": "Acme launching something big", "url": "https://other.com/x", "score": 1, "created_at": "2026-08-30T00:00:00+00:00"}
    ]
    assert reddit_to_candidates(foreign, ACCT, today=TODAY) and all(
        c.signal_type == "intent_3rd_topic" for c in reddit_to_candidates(foreign, ACCT, today=TODAY)
    )


def test_adapter_registered_plan_parse_roundtrip():
    # adapter is registered under its key with the expected tier/cadence
    assert "community_reddit" in SOURCES
    cls = SOURCES["community_reddit"]
    assert cls.key == "community_reddit"
    assert cls.tier == "http"
    assert cls.cadence_hours == 24
    assert cls.requires == ()
    adapter = cls()
    tasks = adapter.plan(ACCT, cursor=None)
    assert tasks and all(t.url == reddit_url(sub) for t, sub in zip(tasks, [t.meta["subreddit"] for t in tasks]))
    assert all(t.url.startswith("https://old.reddit.com/r/") for t in tasks)
    assert all(t.domain == "acme.com" for t in tasks)
    doc = Document(doc_id="r1", source="community_reddit", url=tasks[0].url, body=FIXTURE.read_bytes())
    cands = adapter.parse(doc, ACCT, {"today": TODAY.isoformat()})
    types = {c.signal_type for c in cands}
    assert types == {"intent_3rd_topic", "product_launch"}
    assert any(c.natural_key == "redd:t3_1abcd2" for c in cands)
    assert any(c.natural_key == "reddlaunch:t3_2wxyz9" for c in cands)
    # empty body degrades to no candidates
    assert adapter.parse(Document(doc_id="r2", source="community_reddit", body=b""), ACCT, {"today": TODAY.isoformat()}) == []


def test_module_docstring_states_block_and_disabled_by_default():
    import src.sources.community.reddit as mod

    doc = (mod.__doc__ or "").lower()
    assert "blocked" in doc
    assert "403" in doc or "login" in doc
    assert "disabled-by-default" in doc or "disabled by default" in doc

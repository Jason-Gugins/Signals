import json
from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.community.github import PUSH_BURST_THRESHOLD, parse_events, push_burst_count
from src.sources.community.github_search import parse_search_users, search_users_url


def test_url_shape():
    url = search_users_url("acme corp")
    assert url == "https://api.github.com/search/users?q=acme%20corp%20in%3Alogin&per_page=5"
    assert "per_page=5" in url


def test_parse_search_users():
    body = Path("tests/fixtures/community/github_search_users.json").read_bytes()
    assert "acmecorp" in parse_search_users(body)
    assert parse_search_users(b'{"message": "Validation Failed"}') == []
    assert parse_search_users(b"") == []


def test_recovery_follow_never_guesses():
    from src.sources.community.collector import CommunityGithubSource
    src = CommunityGithubSource()
    acct = Account(domain="acme.com", name="Acme Corp")
    doc = type("D", (), {"body": Path("tests/fixtures/community/github_search_users.json").read_bytes()})()
    meta = {"kind": "search_users", "today": "2026-10-30"}
    tasks = src.follow_tasks(doc, acct, meta)
    assert tasks and all(t.meta.get("kind") in ("repos", "releases", "org") for t in tasks)
    assert all("/acmecorp/" in t.url or "acmecorp" in t.url for t in tasks)
    # a login that matches NO guess -> no follow (never guess)
    doc_bad = type("D", (), {"body": json.dumps({"items": [{"login": "totally-other-org"}]}).encode()})()
    assert src.follow_tasks(doc_bad, acct, meta) == []


def test_parse_events_and_push_burst():
    # Fixture: 12 PushEvents inside the 7-day window (relative to
    # today=2026-10-30), 2 PushEvents outside it, 3 IssueCommentEvents inside.
    body = Path("tests/fixtures/community/github_events.json").read_bytes()
    events = parse_events(body)
    assert len(events) == 17
    assert all("type" in e and "created_at" in e for e in events)
    assert push_burst_count(events, today=date(2026, 10, 30), window_days=7) == 12
    # Tolerant: garbage / non-list JSON bodies parse to [] and count 0.
    assert parse_events(b"not json") == []
    assert parse_events(b'{"message": "Server Error"}') == []
    assert push_burst_count(b"[]", today=date(2026, 10, 30)) == 0


def test_push_burst_signal_emitted():
    # parse() path: kind=="events" doc with >= threshold pushes -> one
    # github_momentum candidate with the monthly natural key.
    from src.sources.community.collector import CommunityGithubSource

    src = CommunityGithubSource()
    acct = Account(domain="acme.com", name="Acme Corp")
    doc = type("D", (), {"body": Path("tests/fixtures/community/github_events.json").read_bytes()})()
    cands = src.parse(doc, acct, {"kind": "events", "today": "2026-10-30"})
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "github_momentum"
    assert c.natural_key == "ghevent:acme.com:202610"
    assert c.observed_at == "2026-10-30"
    assert c.title == "12 pushes in 7 days"
    assert c.confidence == 0.5
    assert c.evidence_data == {"pushes_7d": 12, "threshold": 10}
    assert PUSH_BURST_THRESHOLD == 10
    # Below threshold -> silent.
    small = type(
        "D",
        (),
        {"body": json.dumps([{"type": "PushEvent", "created_at": "2026-10-28T00:00:00Z"}]).encode()},
    )()
    assert src.parse(small, acct, {"kind": "events", "today": "2026-10-30"}) == []
    # Empty body -> silent (parse()'s early return).
    assert src.parse(type("D", (), {"body": b""})(), acct, {"kind": "events", "today": "2026-10-30"}) == []

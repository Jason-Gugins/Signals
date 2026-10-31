import json
from pathlib import Path

from src.core.models import Account
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

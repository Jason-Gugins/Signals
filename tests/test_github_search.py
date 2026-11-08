import json
from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.community.github import PUSH_BURST_THRESHOLD, parse_events, push_burst_count
from src.sources.community.github_search import parse_search_users, search_users_url

# ── plan(): recoverable multi-guess org probes (2026-10-31) ─────────────────


def test_plan_probes_two_guesses_recoverable():
    """plan() probes the FIRST TWO github_org_guess entries (domain label,
    then name slug) as separate org tasks, each flagged recoverable (a 404
    there is an expected "no org at this guess" state, not a failure), plus
    the unchanged search_users task (NOT recoverable — a Search-API failure
    is a real failure worth surfacing)."""
    from src.sources.community.collector import CommunityGithubSource

    src = CommunityGithubSource()
    tasks = src.plan(Account(domain="alpha-sense.com", name="AlphaSense"), None)
    assert len(tasks) == 3
    assert [t.url for t in tasks[:2]] == [
        "https://api.github.com/orgs/alpha-sense",
        "https://api.github.com/orgs/alphasense",
    ]
    for t, guess in zip(tasks[:2], ("alpha-sense", "alphasense")):
        assert t.meta["kind"] == "org"
        assert t.meta["recoverable"] is True
        assert t.meta["guess"] == guess
    # The search task is unchanged: same meta as before (no recoverable).
    assert tasks[2].url == search_users_url("AlphaSense")
    assert tasks[2].meta == {"kind": "search_users"}
    # GITHUB_TOKEN headers are shared by every task (empty dict when unset).
    assert tasks[0].headers == tasks[1].headers == tasks[2].headers

    # One-guess edge: name == domain label dedupes the guesses to one ->
    # a single org probe next to the search task.
    tasks1 = src.plan(Account(domain="acme.com", name="Acme"), None)
    assert len(tasks1) == 2
    assert tasks1[0].url == "https://api.github.com/orgs/acme"
    assert tasks1[0].meta["kind"] == "org"
    assert tasks1[0].meta["recoverable"] is True
    assert tasks1[1].url == search_users_url("Acme")
    assert tasks1[1].meta == {"kind": "search_users"}


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
    # Search-recovery follow tasks carry the recoverable flag too: a 404 on
    # them is an expected miss ("no public code here"), not a failure.
    assert all(t.meta.get("recoverable") is True for t in tasks)
    # a login that matches NO guess -> no follow (never guess)
    doc_bad = type("D", (), {"body": json.dumps({"items": [{"login": "totally-other-org"}]}).encode()})()
    assert src.follow_tasks(doc_bad, acct, meta) == []


def test_org_follow_tasks_all_recoverable():
    """The org-doc follow shape (repos/releases/events) flags EVERY emitted
    task recoverable: GitHub empirically 404s /orgs/{org}/releases for orgs
    with ZERO public repos (2026-10-31) while /orgs/{org}/repos returns 200
    [] — the whole community_github task family treats 404 as an expected
    miss; other statuses stay fatal via the runner's 404-only scoping."""
    from src.sources.community.collector import CommunityGithubSource

    src = CommunityGithubSource()
    acct = Account(domain="acme.com", name="Acme")
    doc = type("D", (), {"body": json.dumps({"login": "acme"}).encode()})()
    tasks = src.follow_tasks(doc, acct, {"kind": "org", "today": "2026-10-31"})
    assert [t.meta["kind"] for t in tasks] == ["repos", "releases", "events"]
    assert all(t.meta["org"] == "acme" for t in tasks)
    assert all(t.meta["recoverable"] is True for t in tasks)
    assert [t.url for t in tasks] == [
        "https://api.github.com/orgs/acme/repos?per_page=100",
        "https://api.github.com/orgs/acme/releases?per_page=20",
        "https://api.github.com/orgs/acme/events?per_page=30",
    ]
    # The kind=None path (a bare org doc without task kind) is the same branch.
    tasks_none = src.follow_tasks(doc, acct, {"today": "2026-10-31"})
    assert [t.meta["kind"] for t in tasks_none] == ["repos", "releases", "events"]
    assert all(t.meta["recoverable"] is True for t in tasks_none)


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

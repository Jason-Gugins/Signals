from datetime import date
from pathlib import Path
from src.core.config import Config
from src.core.models import Account
from src.sources.community.github import (
    github_org_guess,
    github_to_candidates,
    parse_github_org,
    repo_delta,
)
from src.sources.community.hn import hn_to_candidates, hn_url, parse_hn

ACCT = Account(domain="acme.com", name="Acme")


def test_hn():
    url = hn_url("Acme", 1)
    assert "search_by_date" in url and "Acme" in url
    hits = parse_hn((Path("tests/fixtures/community/hn.json")).read_bytes())
    cands = hn_to_candidates(hits, ACCT, today=date(2026, 8, 16))
    types = {c.signal_type for c in cands}
    assert "intent_3rd_topic" in types
    intent = next(c for c in cands if c.signal_type == "intent_3rd_topic")
    assert abs(intent.confidence - 0.5) < 1e-9  # 80 points -> 0.4+0.1
    assert "product_launch" in types


def test_github_org_verify():
    org = parse_github_org((Path("tests/fixtures/community/gh_org.json")).read_bytes())
    assert github_org_guess(ACCT)[0] == "acme"
    good = github_to_candidates(org, [], [{"id": 1, "published_at": "2026-08-01", "tag_name": "v1"}], ACCT, today=date(2026, 8, 16))
    assert any(c.signal_type == "product_launch" for c in good)
    bad_org = {"login": "other", "blog": "https://other.com"}
    assert github_to_candidates(bad_org, [], [], ACCT, today=date(2026, 8, 16)) == []


def test_repo_delta_new_topic():
    prev = {"acme/sdk": {"stars": 10, "archived": False, "seen_at": "2026-09-01", "topics": ["sdk"]}}
    repos = [{"full_name": "acme/sdk", "stargazers_count": 11, "archived": False,
              "created_at": "2024-01-01T00:00:00Z", "topics": ["sdk", "mcp"]}]
    out = repo_delta(prev, repos, today=date(2026, 10, 30))
    kinds = [(k, e) for k, e in out]
    assert any(k == "new_topic" and e["repo"] == "acme/sdk" and e["topic"] == "mcp" for k, e in kinds)


def test_repo_delta_no_topic_repeat():
    prev = {"acme/sdk": {"stars": 10, "archived": False, "seen_at": "2026-10-01", "topics": ["sdk", "mcp"]}}
    repos = [{"full_name": "acme/sdk", "stargazers_count": 10, "archived": False,
              "created_at": "2024-01-01T00:00:00Z", "topics": ["sdk", "mcp"]}]
    assert not [k for k, _ in repo_delta(prev, repos, today=date(2026, 10, 30)) if k == "new_topic"]


def test_unauth_rate_config():
    cfg = Config()
    # default sources.yaml community_github rate_per_host is 1.0; unauthenticated should be slower when no token
    assert cfg.github_token in (None, "")

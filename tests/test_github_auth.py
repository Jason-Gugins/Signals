"""community_github plan() authenticates the GitHub API with GITHUB_TOKEN when set."""
from src.core.models import Account
from src.sources.community.collector import CommunityGithubSource

ACCT = Account(domain="acme.com")


def test_plan_attaches_bearer_token_when_env_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-123")
    task = CommunityGithubSource().plan(ACCT, None)[0]
    assert task.url == "https://api.github.com/orgs/acme"
    assert task.headers.get("Authorization") == "Bearer test-token-123"


def test_plan_omits_authorization_when_env_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    task = CommunityGithubSource().plan(ACCT, None)[0]
    assert "Authorization" not in task.headers

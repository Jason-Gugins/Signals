import os
from datetime import date

from src.sources.base import FetchTask, SourceAdapter
from src.sources.community.github import github_org_guess, github_to_candidates, parse_github_org, parse_releases, parse_repos
from src.sources.community.hn import hn_to_candidates, hn_url, parse_hn
from src.sources.registry import register


@register
class CommunityHnSource(SourceAdapter):
    key = "community_hn"
    tier = "http"
    cadence_hours = 24

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=hn_url(account.name or account.domain, 0), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        return hn_to_candidates(parse_hn(doc.body), account, today=date.fromisoformat(task_meta["today"]))


@register
class CommunityGithubSource(SourceAdapter):
    key = "community_github"
    tier = "http"
    cadence_hours = 48

    def plan(self, account, cursor):
        org = github_org_guess(account)[0]
        # Same env var the config loader reads (src/core/config.py _apply_env_overrides),
        # so config.github_token and the collector always agree. Never log/persist it.
        headers = {}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return [FetchTask(source=self.key, url=f"https://api.github.com/orgs/{org}", domain=account.domain, headers=headers, meta={"kind": "org"})]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        kind = task_meta.get("kind") or "org"
        if kind == "org":
            return []
        org = {"login": task_meta.get("org"), "blog": f"https://{account.domain}"}
        if kind == "releases":
            return github_to_candidates(org, [], parse_releases(doc.body), account, today=today)
        if kind == "repos":
            return github_to_candidates(org, parse_repos(doc.body), [], account, today=today)
        return []

    def follow_tasks(self, doc, account, task_meta):
        if (task_meta or {}).get("kind") not in (None, "org") or not doc.body:
            return []
        org = parse_github_org(doc.body)
        if not org:
            return []
        login = org["login"]
        return [
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{login}/repos?per_page=100", domain=account.domain, meta={"kind": "repos", "org": login}),
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{login}/releases?per_page=20", domain=account.domain, meta={"kind": "releases", "org": login}),
        ]

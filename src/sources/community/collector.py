import os
from datetime import date

from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.community.github import (
    PUSH_BURST_THRESHOLD,
    github_org_guess,
    github_to_candidates,
    parse_events,
    parse_github_org,
    parse_releases,
    parse_repos,
    push_burst_count,
)
from src.sources.community.github_search import parse_search_users, search_users_url
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
        return [
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{org}", domain=account.domain, headers=headers, meta={"kind": "org"}),
            # Search-API org recovery (Task 6): keyless lookup for the real
            # login when the guessed slug is wrong. follow_tasks only follows
            # a returned login that casefolds to a github_org_guess entry.
            FetchTask(source=self.key, url=search_users_url(account.name or account.domain), domain=account.domain, headers=headers, meta={"kind": "search_users"}),
        ]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        kind = task_meta.get("kind") or "org"
        if kind == "org":
            return []
        if kind == "events":
            # Push-burst momentum (Task 8): 10+ pushes in the org's public
            # events within the last 7 days is fresh "heads-down building"
            # evidence the 48h repo-cadence snapshot cannot see. One
            # candidate per account per month (natural key dedupes).
            n = push_burst_count(parse_events(doc.body), today=today)
            if n >= PUSH_BURST_THRESHOLD:
                return [
                    SignalCandidate(
                        "github_momentum",
                        today.isoformat(),
                        f"ghevent:{account.domain}:{today:%Y%m}",
                        title=f"{n} pushes in 7 days",
                        confidence=0.5,
                        evidence_data={"pushes_7d": n, "threshold": PUSH_BURST_THRESHOLD},
                    )
                ]
            return []
        org = {"login": task_meta.get("org"), "blog": f"https://{account.domain}"}
        if kind == "releases":
            return github_to_candidates(org, [], parse_releases(doc.body), account, today=today)
        if kind == "repos":
            return github_to_candidates(org, parse_repos(doc.body), [], account, today=today)
        return []

    def harvest_repos(self, doc, account, task_meta):
        """Runner hook (duck-typed, harvest_tech/harvest_reviews precedent):
        the parsed repo dicts for kind == "repos" docs ONLY — the runner
        diffs them cycle-over-cycle for the github_momentum signal. [] for
        every other kind (org/release docs are not repo snapshots)."""
        if (task_meta or {}).get("kind") != "repos" or not doc.body:
            return []
        return parse_repos(doc.body)

    def follow_tasks(self, doc, account, task_meta):
        kind = (task_meta or {}).get("kind")
        if kind == "search_users":
            # Search-API org recovery (Task 6): follow ONLY a returned login
            # that casefolds to one of github_org_guess's entries — never
            # guess. Recovers DIRECTLY to the repos/releases pair (mirroring
            # the org-doc follow shape below) because the runner's follow
            # budget (max_passes=2) cannot fit the extra org-probe hop; see
            # github_search.py's docstring for the pass math and the accepted
            # tradeoffs.
            if not doc.body:
                return []
            guesses = {g.casefold() for g in github_org_guess(account)}
            hit = next((login for login in parse_search_users(doc.body) if login.casefold() in guesses), None)
            if not hit:
                return []
            return [
                FetchTask(source=self.key, url=f"https://api.github.com/orgs/{hit}/repos?per_page=100", domain=account.domain, meta={"kind": "repos", "org": hit, "recovered_from": "search"}),
                FetchTask(source=self.key, url=f"https://api.github.com/orgs/{hit}/releases?per_page=20", domain=account.domain, meta={"kind": "releases", "org": hit, "recovered_from": "search"}),
            ]
        if kind not in (None, "org") or not doc.body:
            return []
        org = parse_github_org(doc.body)
        if not org:
            return []
        login = org["login"]
        return [
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{login}/repos?per_page=100", domain=account.domain, meta={"kind": "repos", "org": login}),
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{login}/releases?per_page=20", domain=account.domain, meta={"kind": "releases", "org": login}),
            # Push-burst feed (Task 8): 1 extra request/account/48h.
            FetchTask(source=self.key, url=f"https://api.github.com/orgs/{login}/events?per_page=30", domain=account.domain, meta={"kind": "events", "org": login}),
        ]

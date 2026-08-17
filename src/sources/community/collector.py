from src.sources.base import FetchTask, SourceAdapter
from src.sources.community.hn import hn_url
from src.sources.registry import register


@register
class CommunityHnSource(SourceAdapter):
    key = "community_hn"
    tier = "http"
    cadence_hours = 24

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=hn_url(account.name or account.domain, 0), domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []


@register
class CommunityGithubSource(SourceAdapter):
    key = "community_github"
    tier = "http"
    cadence_hours = 48

    def plan(self, account, cursor):
        from src.sources.community.github import github_org_guess
        org = github_org_guess(account)[0]
        return [FetchTask(source=self.key, url=f"https://api.github.com/orgs/{org}", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []

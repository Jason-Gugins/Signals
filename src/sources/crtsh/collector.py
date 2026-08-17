from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register


@register
class CrtshSource(SourceAdapter):
    key = "crtsh"
    tier = "http"
    cadence_hours = 336

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://crt.sh/?q=%25.{account.domain}&output=json", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        try:
            from src.sources.crtsh.subdomains import parse_crtsh
            if doc.body:
                parse_crtsh(doc.body)
        except Exception:
            pass
        return []

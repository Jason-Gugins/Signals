from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.wayback.cdx import cdx_url, parse_cdx, pick_snapshots, snapshot_url


@register
class WaybackSource(SourceAdapter):
    key = "wayback"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=cdx_url(account.domain, from_year=2018), domain=account.domain, meta={"kind": "cdx"})]

    def parse(self, doc, account, task_meta):
        return []

    def follow_tasks(self, doc, account, task_meta):
        if not doc.body:
            return []
        rows = parse_cdx(doc.body)
        out = []
        for ts in pick_snapshots(rows, per_year=2, max_total=12):
            original = next((r.get("original") for r in rows if r.get("timestamp") == ts), f"https://{account.domain}/")
            out.append(
                FetchTask(
                    source=self.key,
                    url=snapshot_url(ts, original),
                    domain=account.domain,
                    meta={"kind": "snapshot"},
                )
            )
        return out

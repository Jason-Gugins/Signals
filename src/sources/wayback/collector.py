from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register
from src.sources.wayback.cdx import cdx_url, parse_cdx, pick_snapshots, snapshot_url
from src.sources.wayback.pricing import diff_pricing


@register
class WaybackSource(SourceAdapter):
    key = "wayback"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=cdx_url(account.domain, from_year=2018), domain=account.domain, meta={"kind": "cdx"})]

    def parse(self, doc, account, task_meta):
        if (task_meta or {}).get("kind") != "pricing":
            return []
        # Conservative: diff against the previously stored pricing html for
        # this domain, carried in task_meta by the runner (meta.setdefault
        # pattern). No previous html -> no signal, never guess.
        prev_html = (task_meta or {}).get("prev_pricing_html")
        if not prev_html:
            return []
        today = (task_meta or {}).get("today")
        if not today:
            return []
        cand = diff_pricing(prev_html, doc.body, domain=account.domain, today=today)
        return [cand] if cand is not None else []

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
        # Pricing follow-up: when any CDX row is a /pricing URL, emit ONE
        # pricing snapshot task (latest timestamp wins) reusing the existing
        # snapshot follow structure — no new fetch tier, no extra network
        # calls beyond this follow task.
        pricing_rows = [
            r
            for r in rows
            if (r.get("original") or "").rstrip("/").lower().endswith("/pricing")
        ]
        if pricing_rows:
            ts = max(r.get("timestamp", "") for r in pricing_rows)
            original = next(
                (r.get("original") for r in pricing_rows if r.get("timestamp") == ts),
                f"https://{account.domain}/pricing",
            )
            out.append(
                FetchTask(
                    source=self.key,
                    url=snapshot_url(ts, original),
                    domain=account.domain,
                    meta={"kind": "pricing"},
                )
            )
        return out

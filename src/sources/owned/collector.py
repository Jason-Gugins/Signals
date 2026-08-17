from pathlib import Path

from src.sources.base import SourceAdapter
from src.sources.owned.ingest import aggregate_intent, load_owned_page_rules, read_drop
from src.sources.registry import register


@register
class OwnedIntentSource(SourceAdapter):
    key = "owned_intent"
    tier = "local"
    cadence_hours = 1

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        rules = load_owned_page_rules()
        inbox = Path(task_meta.get("inbox") or "data/inbox/owned")
        cands = []
        if not inbox.exists():
            return cands
        for path in sorted(inbox.glob("*.csv")) + sorted(inbox.glob("*.jsonl")):
            pairs = aggregate_intent(read_drop(str(path)), today=today, page_rules=rules)
            cands.extend(c for domain, c in pairs if domain == account.domain)
        return cands

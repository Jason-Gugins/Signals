from src.sources.base import SourceAdapter
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

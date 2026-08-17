from src.sources.base import SourceAdapter
from src.sources.registry import register


@register
class MarketplaceG2Source(SourceAdapter):
    key = "marketplace_g2"
    tier = "browser"
    cadence_hours = 168

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

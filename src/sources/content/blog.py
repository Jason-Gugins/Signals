"""First-party blog classification using news rules (no name guard)."""

from __future__ import annotations

from datetime import date

from src.core.models import Account
from src.sources.base import SignalCandidate
from src.sources.news.classify import NEWS_RULES, extract_vars
from src.sources.news.feeds import NewsItem
import re

BLOG_ALLOW = {"product_launch", "award", "certification", "office_open", "content_appearance"}


def blog_to_candidates(items: list[NewsItem], account: Account, *, today: date) -> list[SignalCandidate]:
    out = []
    for item in items:
        text = f"{item.title} {item.summary or ''}"
        hit = None
        for rule in NEWS_RULES:
            if rule.signal_type not in BLOG_ALLOW:
                continue
            if any(re.search(n, text, re.I) for n in rule.negative):
                continue
            if any(re.search(p, text, re.I) for p in rule.patterns):
                hit = rule
                break
        if hit is None:
            continue
        out.append(
            SignalCandidate(
                signal_type=hit.signal_type,
                observed_at=item.published or today.isoformat(),
                natural_key=item.link,
                title=item.title,
                url=item.link,
                confidence=min(0.85, hit.confidence),
                evidence_data=extract_vars(text, hit),
            )
        )
    return out

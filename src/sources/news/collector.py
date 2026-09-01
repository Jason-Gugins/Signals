from dataclasses import asdict
from datetime import date
from typing import Optional

from loguru import logger

from src.core.models import Account, Document
from src.signals.rerank import NullScorer, get_scorer  # noqa: F401  (lazy use; import-safe without ML deps)
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.content.blog import blog_to_candidates
from src.sources.news.classify import _strip_source_attribution, classify_news
from src.sources.news.feeds import NewsItem, bing_news_url, google_news_search_url, google_news_topic_url, google_news_url, parse_feed
from src.sources.news.serp_config import load_google_news_cfg
from src.sources.registry import register


# --------------------------------------------------------------------------
# Optional relevance rerank (plan Task 3). Disabled by default: when the
# rerank gate is off, parse() behaves byte-identically to before — no scorer
# is constructed and items pass through untouched.
#
# Note on evidence: classify_news() builds a fresh SignalCandidate from the
# NewsItem, so relevance evidence attached to the item would be lost. The
# rerank step therefore returns a {link: score} map and parse() copies it
# into candidate.evidence_data["relevance"] — natural_key is never touched.
# --------------------------------------------------------------------------
_RERANK_CFG_CACHE: Optional[object] = None


def _rerank_cfg(task_meta: dict):
    """Resolve the rerank config from task_meta override or config/default.yaml.

    Returns the RerankConfig when rerank is ENABLED, else None (gate off).
    task_meta["rerank"] may carry a RerankConfig or a dict ({enabled, floor});
    absent, the shared Config.load() snapshot (cached) is consulted.
    """
    global _RERANK_CFG_CACHE
    raw = (task_meta or {}).get("rerank")
    if raw is not None:
        if isinstance(raw, dict):
            if not raw.get("enabled", False):
                return None
            from src.core.config import RerankConfig
            fields = RerankConfig.__dataclass_fields__
            return RerankConfig(**{k: v for k, v in raw.items() if k in fields})
        return raw if getattr(raw, "enabled", False) else None
    if _RERANK_CFG_CACHE is None:
        try:
            from src.core.config import Config
            _RERANK_CFG_CACHE = Config.load().rerank
        except Exception as exc:
            # Warn (don't silent-cache a typo): a config problem that
            # permanently disables reranking must be visible to the operator.
            logger.warning("rerank config load failed, rerank disabled: %s", exc)
            from src.core.config import RerankConfig
            _RERANK_CFG_CACHE = RerankConfig()  # conservatively disabled
    return _RERANK_CFG_CACHE if getattr(_RERANK_CFG_CACHE, "enabled", False) else None


def _rerank_items(
    items: list[NewsItem], account: Account, task_meta: dict
) -> tuple[list[NewsItem], dict[str, float]]:
    """Score items against the query; drop below-floor via apply_relevance_floor.

    Returns (kept_items, {link: relevance_score}) — the map is empty when the
    gate is off or no model ran. A model failure can never fail a parse: any
    exception degrades to the unranked item list.
    """
    cfg = _rerank_cfg(task_meta)
    if cfg is None or not items:
        return items, {}
    try:
        from src.signals.rerank import apply_relevance_floor, attach_relevance_evidence

        name = account.name or account.domain
        keyword = task_meta.get("keyword")
        query = f"{name} {keyword}" if keyword else name
        scores = get_scorer().score_pairs(
            query,
            [
                (_strip_source_attribution(it.title or ""), it.summary or "")
                for it in items
            ],
        )
        if scores is None:
            return items, {}  # no model ran — keep everything, no evidence
        # attach_relevance_evidence/apply_relevance_floor operate on dicts;
        # mirror the NewsItems as dicts, filter + annotate, rebuild.
        views = [asdict(it) for it in items]
        kept = apply_relevance_floor(views, scores, floor=cfg.floor)
        kept_scores = [s for s in scores if s is not None and s >= cfg.floor]
        kept = attach_relevance_evidence(kept, kept_scores)
        relevance = {
            d["link"]: d["evidence_data"]["relevance"]
            for d in kept
            if d.get("evidence_data", {}).get("relevance") is not None
        }
        fields = NewsItem.__dataclass_fields__
        return [NewsItem(**{k: d[k] for k in fields}) for d in kept], relevance
    except Exception as exc:
        # Degrade loudly: a persistent model/tokenizer error must not be
        # silent forever — the operator needs to know reranking is off.
        logger.warning("rerank skipped, falling back to unranked items: %s", exc)
        return items, {}


@register
class NewsRssSource(SourceAdapter):
    key = "news_rss"
    tier = "http"
    cadence_hours = 12

    def plan(self, account, cursor):
        name = account.name or account.domain
        return [
            FetchTask(source=self.key, url=google_news_url(name), domain=account.domain, meta={"kind": "gnews"}),
            FetchTask(source=self.key, url=bing_news_url(name), domain=account.domain, meta={"kind": "bing"}),
        ]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        items, relevance = _rerank_items(parse_feed(doc.body), account, task_meta)
        out = []
        for it in items:
            c = classify_news(it, account, today=today)
            if c:
                if it.link in relevance:
                    c.evidence_data["relevance"] = relevance[it.link]
                out.append(c)
        return out


@register
class CompanyFeedSource(SourceAdapter):
    key = "company_feed"
    tier = "http"
    cadence_hours = 24
    requires = ("blog_feed_url",)

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=account.blog_feed_url, domain=account.domain)]

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        return blog_to_candidates(parse_feed(doc.body), account, today=date.fromisoformat(task_meta["today"]))


@register
class GoogleNewsSource(SourceAdapter):
    key = "google_news"
    tier = "http"
    cadence_hours = 12
    emits = ("funding_round", "exec_hire", "exec_departure", "product_launch",
             "layoff", "ma_acquirer", "ma_target", "ipo_filing", "ipo_pricing",
             "office_open", "award", "certification", "earnings_warning",
             "market_consolidation", "competitor_outage")

    # Default topic sections to fetch alongside the keyword search.
    DEFAULT_TOPICS = ("TECHNOLOGY", "BUSINESS")

    def __init__(self):
        self._serp_keywords = (load_google_news_cfg() or {}).get("serp_keywords") or []

    def plan(self, account, cursor):
        name = account.name or account.domain
        tasks = [
            FetchTask(
                source=self.key,
                url=google_news_search_url(f'"{name}"'),
                domain=account.domain,
                meta={"kind": "search", "query": name},
            ),
        ]
        # SERP manipulation: keyword-augmented searches (higher recall).
        # e.g. '"Acme Corp" fundraising', '"Acme Corp" acquisition'
        for kw in self._serp_keywords:
            tasks.append(
                FetchTask(
                    source=self.key,
                    url=google_news_search_url(f'"{name}" {kw}'),
                    domain=account.domain,
                    meta={"kind": "serp", "query": name, "keyword": kw},
                )
            )
        for topic in self.DEFAULT_TOPICS:
            tasks.append(
                FetchTask(
                    source=self.key,
                    url=google_news_topic_url(topic),
                    domain=account.domain,
                    meta={"kind": "topic", "topic": topic},
                )
            )
        return tasks

    def parse(self, doc, account, task_meta):
        if not doc.body:
            return []
        today = date.fromisoformat(task_meta["today"])
        items, relevance = _rerank_items(parse_feed(doc.body), account, task_meta)
        out = []
        for it in items:
            c = classify_news(it, account, today=today)
            if c:
                if it.link in relevance:
                    c.evidence_data["relevance"] = relevance[it.link]
                out.append(c)
        return out

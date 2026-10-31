"""Reddit brand-search adapter (Atom search RSS) — LIVE via scoped override.

Operator decision 2026-10-30: www.reddit.com robots.txt is
`User-agent: * Disallow: /` and the Public Content Policy
(support.reddithelp.com/hc/en-us/articles/26410290525844) restricts automated
collection — and we override both for EXACTLY ONE path (/search.rss, via the
scoped robots_allow entry in config/default.yaml). Rationale, recorded in
config: the feed is Reddit's public syndication surface (HTTP 200 Atom to a
plain descriptive UA, probe 2026-10-30) and volume is a politeness-bounded
trickle — 1-4 quoted-brand queries per account per 72h at rate_per_host 0.2.
Honest failure path: if Reddit starts blocking (403/challenge), Task D3's
live check reverts config/sources.yaml enabled to false.

Scope: quoted brand-name search only (site-wide + up to 3 operator-chosen
subreddits via extra_data["reddit_subreddits"]), weekly window — mirrors the
HN model (find third-party posts mentioning the account). The old old.reddit
HTML parser (synthetic-fixture-only, P2-spike-blocked) was retired with this
rewrite.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.names import normalize_name
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

_LAUNCH_RE = re.compile(r"\b(launch\w*|introducing|announcing)\b", re.I)


def reddit_search_url(name: str, *, subreddit: str | None = None) -> str:
    # casefold before quoting: Reddit search is case-insensitive and the
    # lowercase canonical form is what the URL contract pins
    # (%22acme%20corp%22). The subreddit branch joins restrict_sr=on with an
    # explicit "&" so the q= parameter survives as its own query param.
    q = quote(f'"{name.casefold()}"')
    base = (f"https://www.reddit.com/r/{quote(subreddit)}/search.rss?restrict_sr=on&"
            if subreddit else "https://www.reddit.com/search.rss?")
    return f"{base}q={q}&sort=new&t=week"


def parse_reddit_atom(body: bytes) -> list[dict]:
    """Atom in, post dicts out. PURE. feedparser is already a dependency."""
    import feedparser

    if not body:
        return []
    fp = feedparser.parse(body)
    out: list[dict] = []
    for e in fp.entries:
        guid = e.get("id") or e.get("link") or ""
        pid = guid.rsplit("/", 1)[-1] if guid else None
        tags = e.get("tags") or []
        out.append(
            {
                "id": pid,
                "title": e.get("title"),
                "url": e.get("link"),
                "created_at": e.get("published") or e.get("updated"),
                "subreddit": ((tags[0].get("term") or "").strip() or None) if tags else None,
                "author": e.get("author"),
                "body": e.get("summary") or "",
            }
        )
    return out


def reddit_to_candidates(hits, account: Account, *, today: date) -> list[SignalCandidate]:
    """Mirrors hn_to_candidates: title-or-body brand mention -> intent_3rd_topic
    (static 0.5 — search RSS carries no score); launch regex + own-domain link
    -> product_launch. PURE.

    POSTS ONLY (live-capture drift fix, 2026-10-31): reddit search.rss mixes
    subreddit entries (id ``t5_…``, url = the subreddit home, description may
    mention the brand) in with post entries (id ``t3_…``). A subreddit whose
    sidebar/description mentions the brand is structural noise, not a
    third-party discussion — t5_ entries never produce candidates.
    """
    want = normalize_name(account.name) if account.name else ""
    if not want:
        return []
    seen: set[str] = set()
    out: list[SignalCandidate] = []
    for h in hits:
        pid = h.get("id")
        if not pid or pid in seen or not str(pid).startswith("t3_"):
            continue
        seen.add(pid)
        title = h.get("title") or ""
        blob = normalize_name(f"{title} {h.get('body') or ''}") or ""
        if want not in blob:
            continue
        when = (h.get("created_at") or today.isoformat())[:10]
        out.append(
            SignalCandidate(
                "intent_3rd_topic",
                when,
                f"redd:{pid}",
                title=title,
                url=h.get("url"),
                confidence=0.5,
                evidence_data={"subreddit": h.get("subreddit")},
            )
        )
        if _LAUNCH_RE.search(title):
            host = root_domain(h.get("url"))
            if host and host == account.domain:
                out.append(
                    SignalCandidate(
                        "product_launch",
                        when,
                        f"reddlaunch:{pid}",
                        title=title,
                        url=h.get("url"),
                        confidence=0.6,
                        evidence_data={},
                    )
                )
    return out


@register
class RedditSource(SourceAdapter):
    """community_reddit — live brand search via the scoped /search.rss robots override."""

    key = "community_reddit"
    tier = "http"
    cadence_hours = 72

    def plan(self, account: Account, cursor):
        name = account.name or account.domain
        if not name:
            return []
        subs = list((account.extra_data or {}).get("reddit_subreddits") or [])[:3]
        tasks = [FetchTask(source=self.key, url=reddit_search_url(name), domain=account.domain)]
        tasks += [
            FetchTask(source=self.key, url=reddit_search_url(name, subreddit=s),
                      domain=account.domain, meta={"subreddit": s})
            for s in subs
        ]
        return tasks

    def parse(self, doc, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        return reddit_to_candidates(
            parse_reddit_atom(doc.body),
            account,
            today=date.fromisoformat(task_meta["today"]),
        )

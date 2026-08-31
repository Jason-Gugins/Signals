"""Reddit community parser (old.reddit listing markup) — STUB, fixture-only.

LIVE FETCHING IS BLOCKED (P2 spike 2026-08-31, see data/probe/P2_SOURCE_SPIKE.md):
``www.reddit.com`` returns a 403 network-security block page on the JSON
endpoint, and ``old.reddit.com`` serves a login interstitial with zero post
markup when unauthenticated. No post content is obtainable from either host,
so this module is a synthetic-fixture stub: the selectors below are validated
ONLY against ``tests/fixtures/community/reddit_post_list.html`` (synthetic
old.reddit-style markup written from known structure, not captured live) and
MUST be re-verified against real markup before any live attempt.

The ``RedditSource`` adapter (key ``community_reddit``) is therefore
DISABLED-BY-DEFAULT — ``config/sources.yaml`` pins ``enabled: false`` and it
must stay that way until a live capture path exists.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional
from urllib.parse import quote

from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.names import normalize_name
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.registry import register

# Subreddits probed in the spike / generic SMB-research communities. Accounts
# may override via extra_data["reddit_subreddits"].
DEFAULT_SUBREDDITS: tuple[str, ...] = ("sales", "saas", "startups")

_LAUNCH_RE = re.compile(r"\b(launch\w*|introducing|announcing)\b", re.I)


def reddit_url(subreddit: str) -> str:
    return f"https://old.reddit.com/r/{quote(subreddit)}/"


def parse_reddit_posts(html: str) -> list[dict]:
    """Parse an old.reddit-style listing page into post dicts. PURE.

    Selector surface (synthetic-fixture contract only — see module docstring):

    - Post container: ``div.thing`` (carries ``data-fullname="t3_<id>"``).
    - Title: ``a.title`` — text is the post title, ``href`` the link URL.
    - Score: ``span.score`` text (integer).
    - Timestamp: ``<time datetime="...">`` ISO-8601 attribute.
    - Subreddit/author: ``data-subreddit`` / ``data-author`` on the container.

    bs4 is imported lazily inside the function; no I/O, no clock reads.
    """
    from bs4 import BeautifulSoup

    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for thing in soup.select("div.thing"):
        fullname = thing.get("data-fullname")
        if not fullname:
            classes = " ".join(thing.get("class") or [])
            m = re.search(r"\b(?:id-|thing_)?(t3_[A-Za-z0-9]+)\b", classes)
            fullname = m.group(1) if m else None
        if not fullname:
            continue
        title_el = thing.select_one("a.title")
        score_el = thing.select_one("span.score")
        time_el = thing.select_one("time[datetime]")
        score_text = (score_el.get_text(strip=True) if score_el else "") or "0"
        try:
            score = int(score_text)
        except ValueError:
            score = 0
        out.append(
            {
                "id": fullname,
                "title": title_el.get_text(strip=True) if title_el else None,
                "url": title_el.get("href") if title_el else None,
                "score": score,
                "created_at": time_el.get("datetime") if time_el else None,
                "subreddit": thing.get("data-subreddit"),
                "author": thing.get("data-author"),
            }
        )
    return out


def reddit_to_candidates(hits, account: Account, *, today: date) -> list[SignalCandidate]:
    """Post dicts -> SignalCandidates. Mirrors hn_to_candidates. PURE."""
    want = normalize_name(account.name) if account.name else ""
    seen: set[str] = set()
    out: list[SignalCandidate] = []
    for h in hits:
        pid = h.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = h.get("title") or ""
        if want and want not in (normalize_name(title) or ""):
            continue
        score = int(h.get("score") or 0)
        conf = min(0.7, 0.4 + 0.1 * (score // 50))
        out.append(
            SignalCandidate(
                signal_type="intent_3rd_topic",
                observed_at=(h.get("created_at") or today.isoformat())[:10],
                natural_key=f"redd:{pid}",
                title=title,
                url=h.get("url"),
                confidence=conf,
                evidence_data={"score": score, "subreddit": h.get("subreddit")},
            )
        )
        if _LAUNCH_RE.search(title):
            host = root_domain(h.get("url"))
            if host and host == account.domain:
                out.append(
                    SignalCandidate(
                        signal_type="product_launch",
                        observed_at=(h.get("created_at") or today.isoformat())[:10],
                        natural_key=f"reddlaunch:{pid}",
                        title=title,
                        url=h.get("url"),
                        confidence=0.6,
                        evidence_data={},
                    )
                )
    return out


@register
class RedditSource(SourceAdapter):
    """community_reddit adapter — disabled-by-default (live fetching blocked)."""

    key = "community_reddit"
    tier = "http"
    cadence_hours = 24
    requires: tuple[str, ...] = ()

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        subs = (account.extra_data or {}).get("reddit_subreddits") or DEFAULT_SUBREDDITS
        return [
            FetchTask(source=self.key, url=reddit_url(sub), domain=account.domain, meta={"subreddit": sub})
            for sub in subs
        ]

    def parse(self, doc, account: Account, task_meta: dict) -> list[SignalCandidate]:
        if not doc.body:
            return []
        html = doc.body.decode("utf-8", "replace") if isinstance(doc.body, (bytes, bytearray)) else doc.body
        return reddit_to_candidates(
            parse_reddit_posts(html),
            account,
            today=date.fromisoformat(task_meta["today"]),
        )

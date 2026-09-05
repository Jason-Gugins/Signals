"""App Store customer-reviews source (iTunes RSS JSON).

Spike verdict (data/probe/P2_SOURCE_SPIKE.md, Task 6):
- **App Store: GO plain-fetch.** ``itunes.apple.com`` serves the
  customer-reviews feed as public, server-rendered JSON with no anti-bot
  challenge; works with plain curl_cffi at http tier.
- **Play Store: NO-GO / synthetic-only.** The Play web reviews page is
  JS-rendered and the probed package returned 404. Do NOT build a Play
  fetcher against this module — any future Play capture needs a fresh
  browser-tier probe first.

Feed shape (verified against the live Slack iOS fixture)::

    {"feed": {"entry": [
        {"title": {"label": "Loved the old slack, like the new"},
         "im:rating": {"label": "4"},
         "author": {"name": {"label": "vshultz"}},
         "content": {"label": "Listen, the old Slack was the best..."},
         "id": {"label": "14490360492"},          # stable review id
         "updated": {"label": "2026-08-30T08:36:05-07:00"},
         "link": {"attributes": {"href": "https://itunes.apple.com/..."}}},
        ...]}}

A dead app id returns HTTP 200 with an empty shell feed that has **no
``entry`` key** — that is the natural "no reviews" sentinel (the parser
returns ``[]``), not an error.
"""

from __future__ import annotations

import json
from typing import Optional

from src.core.models import Account, Document
from src.sources.base import FetchTask, SourceAdapter
from src.sources.registry import register

COUNTRY_DEFAULT = "us"


def itunes_reviews_url(app_store_id: str, country: str = COUNTRY_DEFAULT) -> str:
    """Build the iTunes customer-reviews RSS JSON URL (most-recent first)."""
    return (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"id={app_store_id}/sortby=mostrecent/json"
    )


def parse_itunes_reviews(body: bytes) -> list[dict]:
    """PURE. Parse an iTunes RSS reviews JSON body into flat review dicts.

    Returns a list of dicts shaped like the review payloads with a stable
    ``review_id`` (the feed entry ``id.label``) and integer ``rating``.
    Empty body, unparseable JSON, or a shell feed with no ``entry`` key
    (the dead-app-id sentinel) yields ``[]`` — not an error.
    """
    if not body:
        return []
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    feed = data.get("feed")
    if not isinstance(feed, dict):
        return []
    entries = feed.get("entry")
    if not entries:
        return []
    if isinstance(entries, dict):  # single-entry feed ships a bare object
        entries = [entries]
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        entry_id = (e.get("id") or {}).get("label")
        if not entry_id:
            continue
        rating_raw = (e.get("im:rating") or {}).get("label")
        try:
            rating = int(rating_raw) if rating_raw is not None else None
        except (TypeError, ValueError):
            rating = None
        content = (e.get("content") or {}).get("label")
        updated = (e.get("updated") or {}).get("label")
        link = ((e.get("link") or {}).get("attributes") or {}).get("href")
        version = (e.get("im:version") or {}).get("label")
        out.append(
            {
                "review_id": entry_id,
                "rating": rating,
                "author": (e.get("author") or {}).get("name", {}).get("label"),
                "title": (e.get("title") or {}).get("label"),
                "body": content,
                "posted_at": updated,
                "review_url": link,
                "version": version,
            }
        )
    return out


@register
class AppStoreReviewSource(SourceAdapter):
    """iTunes customer-reviews RSS adapter (http tier, weekly cadence).

    Requires the ``app_store_id`` account field; the runner dispatches
    ``harvest_reviews`` per adapter key and ``upsert_appstore_reviews``
    (this module) persists them to the shared ``g2_reviews`` table with
    ``source='appstore'``.
    """

    key = "appstore_reviews"
    tier = "http"
    cadence_hours = 168
    requires = ("app_store_id",)
    emits = ("appstore_reviews",)

    def plan(self, account: Account, cursor: Optional[str]) -> list[FetchTask]:
        """PURE. One fetch task per app_store_id (comma-separated list OK)."""
        raw = account.app_store_id
        if not raw:
            return []
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        return [
            FetchTask(
                source=self.key,
                url=itunes_reviews_url(app_id),
                domain=account.domain,
                # product_slug: the app id IS the stable per-app slug (same
                # role as marketplace product slugs) — the runner keys its
                # review_harvests filing (review-velocity trend stats) on it.
                meta={"kind": "reviews", "app_store_id": app_id, "product_slug": str(app_id)},
            )
            for app_id in ids
        ]

    def parse(self, doc: Document, account: Account, task_meta: dict) -> list:
        """Reviews are harvested into g2_reviews, not emitted as signals."""
        return []

    def harvest_reviews(self, doc: Document, account: Account, task_meta: dict) -> list[dict]:
        """PURE. Feed bytes in, app-store review dicts out.

        Each dict is stamped with the ``app_store_id`` from the task meta so
        :func:`upsert_appstore_reviews` can build the namespaced review_id
        and product_slug.
        """
        app_store_id = (task_meta or {}).get("app_store_id") or account.app_store_id
        out = parse_itunes_reviews(doc.body or b"")
        for r in out:
            r["app_store_id"] = app_store_id
        return out


def upsert_appstore_reviews(db, reviews: list[dict], *, now: str,
                            raw_ref: Optional[str] = None,
                            app_store_id: Optional[str] = None) -> tuple[int, int]:
    """Upsert App Store review dicts into the shared g2_reviews table.

    Writes ``source='appstore'`` rows with ``product_slug =
    'appstore:<app_store_id>'`` and ``review_id = '<app_store_id>:<id.label>'``
    so the App Store id space cannot collide with G2/Capterra/TrustRadius
    ids. ``app_store_id`` (or per-review ``r["app_store_id"]``) keys the
    namespace; reviews lacking both are skipped. Returns (new, updated).

    The per-review ``version`` captured by parse_itunes_reviews has no
    g2_reviews column and deliberately gets none (no schema migration): the
    upsert ignores it, so it only rides in the review dict (trend/evidence
    path) until a column is actually warranted.
    """
    new, updated = 0, 0
    for r in reviews:
        asid = r.get("app_store_id") or app_store_id
        if not asid:
            continue
        review_id = f"{asid}:{r.get('review_id')}"
        existing = db.one(
            "SELECT first_seen_at FROM g2_reviews WHERE review_id=?", (review_id,)
        )
        db.upsert(
            "g2_reviews",
            {
                "review_id": review_id,
                "product_slug": f"appstore:{asid}",
                "reviewer_name": r.get("author"),
                "reviewer_title": None,
                "reviewer_company_size": None,
                "rating": r.get("rating"),
                "review_title": r.get("title"),
                "review_body": r.get("body"),
                "pros": None,
                "cons": None,
                "posted_at": r.get("posted_at"),
                "review_url": r.get("review_url"),
                "verified_reviewer": 0,
                "review_source": "appstore",
                "nps_score": None,
                "helpful_votes": None,
                "source": "appstore",
                "first_seen_at": existing["first_seen_at"] if existing else now,
                "last_seen_at": now,
                "raw_ref": raw_ref,
            },
            pk=("review_id",),
            # Only bump freshness metadata on re-observation. review_body and
            # rating are NOT in the overwrite set: a later feed entry with a
            # missing/unparseable content or rating must not NULL out what a
            # previous cycle stored (G2 path COALESCEs for the same reason).
            overwrite={"last_seen_at", "raw_ref"},
        )
        if existing:
            updated += 1
        else:
            new += 1
    return new, updated

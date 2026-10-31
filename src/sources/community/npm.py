"""npm registry search parser. PURE. Keyless public endpoints:

  search: https://registry.npmjs.org/-/v1/search?text=<q>&size=20
  doc:    https://registry.npmjs.org/<pkg>

downloads.monthly/weekly come INLINE on search objects (no second call).
Attribution rule (never-guess): a package is the account's ONLY when its
repository URL is github.com and the org path segment exactly equals one of
github_org_guess(account). Name-collision packages without a matching
repository field are ignored.
"""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import quote

from src.core.models import Account
from src.core.textutil import to_iso_date
from src.sources.base import SignalCandidate
from src.sources.community.github import github_org_guess

NEW_PACKAGE_WINDOW_DAYS = 90
DOWNLOAD_SURGE_FRACTION = 0.25
DOWNLOAD_SURGE_MIN_ABS = 50
DOWNLOAD_SURGE_FLOOR = 100
# npm momentum state home (Task C3): the per-domain stats-file pattern of
# community/stats.py — whose DEFAULT_STATS_PATH is the github-keyed
# data/github/stats.json — so the npm default lives beside the npm momentum
# constants. Data only (no I/O in this module): the runner loads/saves the
# state file under this path; tests redirect it.
DEFAULT_STATS_PATH = "data/npm/stats.json"


def npm_search_url(name: str) -> str:
    # quote() not quote_plus(): spaces must encode as %20 (pinned by test_url),
    # and '+' in a raw search term would otherwise mean a literal plus.
    return f"https://registry.npmjs.org/-/v1/search?text={quote(name)}&size=20"


def parse_npm_search(body: bytes) -> list[dict]:
    try:
        raw = json.loads(body)
    except ValueError:
        return []
    out: list[dict] = []
    for obj in raw.get("objects") or []:
        p = obj.get("package") or {}
        links = p.get("links") or {}
        out.append({
            "name": p.get("name"),
            "version": p.get("version"),
            "date": p.get("date"),
            "description": p.get("description"),
            "downloads_monthly": int((obj.get("downloads") or {}).get("monthly") or 0),
            "downloads_weekly": int((obj.get("downloads") or {}).get("weekly") or 0),
            "repository": links.get("repository"),
            "publisher": (p.get("publisher") or {}).get("username"),
        })
    return out


def owned_packages(pkgs: list[dict], account: Account) -> list[dict]:
    """Strong-arm attribution: repository URL is github.com AND its org path
    segment casefolds to a github_org_guess entry. PURE."""
    guesses = {g.casefold() for g in github_org_guess(account)}
    out = []
    for p in pkgs:
        repo = p.get("repository") or ""
        if "github.com/" not in repo:
            continue
        tail = repo.split("github.com/", 1)[1]
        org = tail.split("/", 1)[0].rstrip(".git").casefold()
        if org in guesses:
            out.append(p)
    return out


def npm_to_candidates(pkgs: list[dict], account: Account, *, today: date) -> list[SignalCandidate]:
    """Third-party packages whose name/description mention the brand ->
    intent_3rd_topic (the HN analog: third parties talking about the account).
    Own packages are excluded here — they surface via momentum (Task C3)."""
    from src.identity.names import normalize_name

    want = normalize_name(account.name) if account.name else ""
    if not want:
        return []
    owned = {(p.get("name") or "").casefold() for p in owned_packages(pkgs, account)}
    out: list[SignalCandidate] = []
    seen: set[str] = set()
    for p in pkgs:
        name = p.get("name") or ""
        if not name or name.casefold() in owned or name in seen:
            continue
        seen.add(name)
        blob = normalize_name(f"{name} {p.get('description') or ''}") or ""
        if want not in blob:
            continue
        out.append(SignalCandidate(
            "intent_3rd_topic", today.isoformat(), f"npm:{name}",
            title=name, url=f"https://www.npmjs.com/package/{quote(name)}",
            confidence=0.4,
            evidence_data={"downloads_monthly": p.get("downloads_monthly"),
                           "description": (p.get("description") or "")[:200]}))
    return out


def npm_delta(prev: dict, pkgs: list[dict], *, today: date) -> list[tuple[str, dict]]:
    """Cycle-over-cycle package momentum. prev: {name: {"version","downloads_monthly","seen_at"}}.
    new_package: absent from prev AND last publish within NEW_PACKAGE_WINDOW_DAYS
    (old packages newly seen by us are not bets). downloads_surge: prior row and
    growth >= max(fraction*prev, min_abs) with prev >= DOWNLOAD_SURGE_FLOOR."""
    out: list[tuple[str, dict]] = []
    for p in pkgs if isinstance(pkgs, list) else []:
        name = str(p.get("name") or "")
        if not name:
            continue
        dm = int(p.get("downloads_monthly") or 0)
        row = prev.get(name)
        if row is None:
            published = to_iso_date(p.get("date"))
            fresh = True
            if published:
                try:
                    fresh = (today - date.fromisoformat(published)).days <= NEW_PACKAGE_WINDOW_DAYS
                except ValueError:
                    fresh = True
            if fresh:
                out.append(("new_package", {"name": name, "version": p.get("version"),
                                            "published": published or None}))
            continue
        prev_dm = int(row.get("downloads_monthly") or 0)
        if prev_dm >= DOWNLOAD_SURGE_FLOOR and dm - prev_dm >= max(DOWNLOAD_SURGE_FRACTION * prev_dm, DOWNLOAD_SURGE_MIN_ABS):
            out.append(("downloads_surge", {"name": name, "downloads": dm, "downloads_prev": prev_dm,
                                            "detail": f"monthly downloads {prev_dm} -> {dm}"}))
    return out

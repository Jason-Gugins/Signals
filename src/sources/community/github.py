"""GitHub org/repo/release parsers. PURE."""

from __future__ import annotations

import json
from datetime import date

from src.core.models import Account
from src.core.textutil import slugify, to_iso_date
from src.identity.domains import root_domain
from src.sources.base import SignalCandidate


def github_org_guess(account: Account) -> list[str]:
    label = account.domain.split(".")[0]
    guesses = [label]
    if account.name:
        guesses.append(slugify(account.name))
        guesses.append(account.name.replace(" ", ""))
    # unique preserve order
    seen = []
    for g in guesses:
        gl = g.casefold()
        if gl and gl not in seen:
            seen.append(gl)
    return seen


def parse_github_org(body: bytes) -> dict | None:
    raw = json.loads(body)
    if not raw or raw.get("message") == "Not Found":
        return None
    return {"login": raw.get("login"), "blog": raw.get("blog"), "html_url": raw.get("html_url")}


def parse_repos(body: bytes) -> list[dict]:
    raw = json.loads(body)
    return raw if isinstance(raw, list) else []


def parse_releases(body: bytes) -> list[dict]:
    raw = json.loads(body)
    return raw if isinstance(raw, list) else []


def _one_year_ago(today: date) -> date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # Feb 29 -> Feb 28 on non-leap years
        return today.replace(year=today.year - 1, day=28)


def github_to_candidates(org, repos, releases, account: Account, *, today: date) -> list[SignalCandidate]:
    if org:
        blog = root_domain(org.get("blog"))
        if org.get("blog") and blog and blog != account.domain:
            return []
    out = []
    for rel in releases:
        published = to_iso_date(rel.get("published_at") or rel.get("created_at"))
        if not published:
            continue
        age = (today - date.fromisoformat(published)).days
        if age <= 90:
            out.append(
                SignalCandidate(
                    signal_type="product_launch",
                    observed_at=published,
                    natural_key=f"ghrel:{rel.get('id') or rel.get('tag_name')}",
                    title=rel.get("name") or rel.get("tag_name"),
                    url=rel.get("html_url"),
                    confidence=0.7,
                    evidence_data={},
                )
            )
    pushed = []
    for repo in repos:
        p = to_iso_date(repo.get("pushed_at"))
        if p:
            pushed.append(date.fromisoformat(p))
    if repos and (not pushed or max(pushed) < _one_year_ago(today)):
        # no push in the last year
        out.append(
            SignalCandidate(
                signal_type="stagnation",
                observed_at=today.isoformat(),
                natural_key=f"ghstale:{account.domain}:{today.year}",
                title="No GitHub pushes in 365 days",
                confidence=0.4,
                evidence_data={},
            )
        )
    return out

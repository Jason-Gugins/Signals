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


# Momentum thresholds: a repo counts as a new product bet within 90 days of
# creation; star growth must clear max(25% of prev, 10 absolute) so tiny
# orgs (floor) and big orgs (percentage) both need real movement.
NEW_REPO_WINDOW_DAYS = 90
STAR_SURGE_MIN_FRACTION = 0.25
STAR_SURGE_MIN_ABSOLUTE = 10


def repo_delta(prev_stats: dict, repos: list[dict], *, today: date) -> list[tuple[str, dict]]:
    """Cycle-over-cycle repo momentum diff. PURE — ``today`` is injected.

    ``prev_stats`` maps full_name -> {"stars": int, "archived": bool,
    "seen_at": str} (the runner's per-domain slice of the stats file; None/{}
    on the first observed cycle). Returns (momentum_kind, evidence) tuples:

    - ``new_repo``: full_name absent from prev_stats AND created_at within
      NEW_REPO_WINDOW_DAYS of ``today`` — or with no parseable created_at
      (the repo is new to us either way). Old repos seen for the first time
      are not a fresh bet and stay silent.
    - ``star_surge``: prior row exists and stars grew by
      >= max(STAR_SURGE_MIN_FRACTION of prev, STAR_SURGE_MIN_ABSOLUTE).
    - ``archived``: archived is truthy and the prior row was not archived.

    evidence carries repo (full_name), stars, stars_prev and a human
    ``detail`` one-liner for the evidence template.
    """
    out: list[tuple[str, dict]] = []
    prev = prev_stats if isinstance(prev_stats, dict) else {}
    for repo in repos if isinstance(repos, list) else []:
        name = str(repo.get("full_name") or "")
        if not name:
            continue
        stars = int(repo.get("stargazers_count") or 0)
        archived = bool(repo.get("archived"))
        row = prev.get(name)
        if row is None:
            created = to_iso_date(repo.get("created_at"))
            fresh = True
            if created:
                try:
                    fresh = (today - date.fromisoformat(created)).days <= NEW_REPO_WINDOW_DAYS
                except ValueError:  # pragma: no cover - to_iso_date already validates
                    fresh = True
            if fresh:
                detail = f"repository created {created}" if created else "repository is new to us (no parseable creation date)"
                out.append(("new_repo", {"repo": name, "stars": stars, "stars_prev": None, "detail": detail}))
            continue
        stars_prev = int(row.get("stars") or 0)
        if stars - stars_prev >= max(STAR_SURGE_MIN_FRACTION * stars_prev, STAR_SURGE_MIN_ABSOLUTE):
            out.append(
                (
                    "star_surge",
                    {
                        "repo": name,
                        "stars": stars,
                        "stars_prev": stars_prev,
                        "detail": f"stars {stars_prev} -> {stars} (+{stars - stars_prev})",
                    },
                )
            )
        if archived and not row.get("archived"):
            out.append(
                (
                    "archived",
                    {
                        "repo": name,
                        "stars": stars,
                        "stars_prev": stars_prev,
                        "detail": f"repository archived ({stars} stars)",
                    },
                )
            )
    return out


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

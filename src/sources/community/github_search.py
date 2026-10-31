"""GitHub Search-API org recovery. PURE parsers + URL builders.

Fixes the silent-empty failure mode: ``github_org_guess`` misses when the org
login is neither the domain label nor the name slug. The Search API is
keyless (10 req/min unauth, 30/min with GITHUB_TOKEN) — one extra request per
account per cycle is well inside the politeness floors. Never-guess rule: a
recovered login is only followed when it EXACTLY matches (casefold) a
``github_org_guess`` entry; a search result that matches no guess yields no
follow tasks at all.

Recovery shape (decision delegated by plan Task 6, decided by reading the
runner): the search doc's ``follow_tasks`` recover DIRECTLY to the
repos/releases pair for the matched login — mirroring the org doc's existing
follow shape — instead of recovering to an org probe first. Reason: the
runner's follow budget cannot fit the org-probe chain. ``max_passes``
defaults to 2 for community_github (``_run_pair`` default; not a
``marketplace_*`` source, no ``follow_passes`` hook), and follow tasks
collected during pass N execute in pass N+1 only when ``N + 1 < max_passes``
(runner: ``if follow and pass_i + 1 < max_passes``). The chain
org(404)+search(pass 0) -> recovered org probe (pass 1) -> repos/releases
(pass 2) would drop its last hop at that boundary, so the org-probe shape can
never yield candidates within the budget; the direct shape fits (repos and
releases are fetched in pass 1). Accepted tradeoff: recovering directly skips
the org doc's blog-field mismatch guard in ``github_to_candidates`` — parse()
synthesizes ``blog=https://{domain}`` for follow docs anyway, so the guard
never fires on the follow path; the never-guess exact-match rule is the only
attribution gate.

Known accepted redundancy: when the org probe SUCCEEDS, the search doc's
follow may re-emit the same repos/releases fetch — conditional GET turns that
into a 304; no cross-task state is added to avoid it (YAGNI).

Known limitation (honest note, 2026-10-30): the live runner raises on a
failed fetch (``if not result.ok ... raise`` in its pass loop) BEFORE follow
tasks are queued, so a hard-404 org probe still fails the pair for that
cycle; firing the recovery on that trigger additionally requires the runner
to tolerate the failed probe — a runner.py change outside Task 6's file
scope. The ``follow_tasks`` branch here is the recovery contract this module
pins.
"""
from __future__ import annotations

import json
from urllib.parse import quote


def search_users_url(name: str) -> str:
    """Keyless Search-API user lookup scoped to logins, top 5 hits.

    Spaces encode as %20 (``quote``, not ``quote_plus``) so the built URL
    matches the pinned contract ``q=<name>%20in%3Alogin&per_page=5``.
    """
    return f"https://api.github.com/search/users?q={quote(name, safe='')}%20in%3Alogin&per_page=5"


def parse_search_users(body: bytes) -> list[str]:
    """Search-API response body -> ordered logins. Tolerant: [] on empty,
    invalid JSON, or a non-dict body; items without a string login are
    skipped (Validation Failed responses have no items)."""
    try:
        raw = json.loads(body)
    except ValueError:
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("items")
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        login = it.get("login")
        if isinstance(login, str) and login:
            out.append(login)
    return out

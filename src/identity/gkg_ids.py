"""Discover company domains from Google Knowledge Graph entities.

Waterfall stage 3 of the Clay "find the company" clone (plan T3b): the
credential-gated GKG client (EKG default, legacy kgsearch fallback — see
gkg_client.py). Intended dispatch: wired into the resolve waterfall by the
parent as a name->domain resolver (this module does not self-dispatch). One
Search call per name; EKG has no pagination, so one call is one candidate set.

Pick is domain-keyed: each entity url is reduced via root_domain() (subdomains
and deep links collapse onto the apex, junk urls vanish) and scored with
name-vs-domain token overlap plus an exact-name bonus. Decision over DISTINCT
domains: zero -> no_match, exactly one -> resolved, more than one -> ambiguous
with all candidates ranked by score (display-only, never auto-picks —
never-guess contract). `resultScore` from either backend is never read.

Parsers/picker are pure; the resolver is network-backed via the injected
KnowledgeGraphClient (token fetching and HTTP are injectable there, so the
offline test lane never touches google-auth or the network).
"""

from __future__ import annotations

from loguru import logger

from src.identity.domains import root_domain
from src.identity.gkg_client import KnowledgeGraphClient
from src.identity.names import name_matches_domain


def _no_match() -> dict:
    return {"status": "no_match", "domain": None, "candidates": []}


def _score_entity(entity: dict, name: str, domain: str | None) -> int:
    score = 0
    if domain and name_matches_domain(name, domain):
        score += 2  # two-way token overlap between query name and domain label
    entity_name = str(entity.get("name") or "").strip()
    if entity_name and entity_name.casefold() == (name or "").strip().casefold():
        score += 1  # exact name match
    return score


def pick_gkg_candidate(entities: list[dict], name: str) -> dict:
    """Root-domain filter, score and decide from client entities (pure).

    `entities` items carry {"name", "url", "entity_id", "types"}. Every url is
    reduced to its root domain (junk urls drop out); entities yielding the same
    domain are deduped to the best-scoring one. Decision over distinct domains:
    zero -> no_match, exactly one -> resolved, more than one -> ambiguous with
    ALL candidates ranked by score (never auto-picks the top one).
    """
    by_domain: dict[str, dict] = {}
    for entity in entities:
        url = entity.get("url")
        domain = root_domain(url if isinstance(url, str) else None)
        if not domain:
            continue
        candidate = {
            "entity_id": entity.get("entity_id"),
            "name": entity.get("name"),
            "domain": domain,
            "url": url,
            "score": _score_entity(entity, name, domain),
        }
        existing = by_domain.get(domain)
        if existing is None or candidate["score"] > existing["score"]:
            by_domain[domain] = candidate
    if not by_domain:
        return _no_match()
    candidates = sorted(by_domain.values(), key=lambda c: c["score"], reverse=True)
    if len(candidates) == 1:
        return {
            "status": "resolved",
            "domain": candidates[0]["domain"],
            "candidates": candidates,
        }
    return {"status": "ambiguous", "domain": None, "candidates": candidates}


class GkgDomainResolver:
    """Name -> company domain from the credential-gated GKG backends.

    `client` is injectable for tests (default: a KnowledgeGraphClient built
    from env credentials). `registry` is accepted for house-style parity with
    the other identity resolvers; this module never writes to it — the
    waterfall wiring layer owns persistence and the 2-source agreement rule
    (never-guess contract).
    """

    def __init__(self, client: KnowledgeGraphClient | None = None, registry=None):
        self.client = client if client is not None else KnowledgeGraphClient()
        self.registry = registry

    def discover(self, name: str) -> dict:
        """Name -> {"status", "domain", "candidates"}. Never raises.

        Client "unconfigured" (no credentials for the selected backend) and
        "error" (credential/HTTP/parse failure) pass through as statuses —
        the stage no-ops, it does not become a no_match.
        """
        try:
            return self._discover(name)
        except Exception as exc:  # broken injected client never raises out
            logger.warning("gkg_ids: discovery failed for {!r}: {}", name, exc)
            return _no_match()

    def resolve_all(self, names: list[str]) -> dict[str, dict]:
        """Batch entry with per-item try/except isolation (resolve_all style)."""
        out: dict[str, dict] = {}
        for name in names:
            try:
                out[name] = self.discover(name)
            except Exception as exc:  # one failing name never kills the pass
                logger.warning("gkg_ids: resolve failed for {!r}: {}", name, exc)
                out[name] = _no_match()
        return out

    def _discover(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return _no_match()
        result = self.client.search(term)
        status = result.get("status")
        if status == "unconfigured":
            return {"status": "unconfigured", "domain": None, "candidates": []}
        if status == "error":
            return {
                "status": "error",
                "domain": None,
                "candidates": [],
                "error": str(result.get("error") or "unknown error"),
            }
        return pick_gkg_candidate(result.get("entities") or [], term)

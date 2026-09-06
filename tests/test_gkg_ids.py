"""Tests for the GKG domain discovery resolver (fake client, no network)."""

from __future__ import annotations

from src.identity.gkg_ids import GkgDomainResolver, pick_gkg_candidate


class FakeClient:
    """Injectable KnowledgeGraphClient stand-in returning canned results."""

    def __init__(self, result: dict | None = None, error: Exception | None = None):
        self.result = result or {}
        self.error = error
        self.calls: list[str] = []

    def search(self, name: str) -> dict:
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.result


class FakeRegistry:
    def __init__(self):
        self.upserts: list = []

    def upsert(self, account, *, source=None):
        self.upserts.append((account, source))
        return account


def _ekg_ok(entities: list[dict]) -> dict:
    return {"backend": "ekg", "status": "ok", "entities": entities, "error": None}


def _entity(name: str, url: str | None, entity_id: str = "c_1") -> dict:
    return {"name": name, "url": url, "entity_id": entity_id, "types": ["Organization"]}


def test_resolved_single_distinct_domain():
    client = FakeClient(_ekg_ok([_entity("Stripe", "https://stripe.com/")]))
    out = GkgDomainResolver(client).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert out["candidates"] == [
        {
            "entity_id": "c_1",
            "name": "Stripe",
            "domain": "stripe.com",
            "url": "https://stripe.com/",
            "score": 3,  # +2 name-vs-domain token overlap, +1 exact name match
        }
    ]
    assert client.calls == ["Stripe"]


def test_ambiguous_two_domains_ranked_but_never_picked():
    entities = [
        _entity("Acme", "https://acme.com/", "c_a"),
        _entity("Acme Systems", "https://example-systems.it/", "c_b"),
    ]
    out = GkgDomainResolver(FakeClient(_ekg_ok(entities))).discover("Acme")
    assert out["status"] == "ambiguous"
    assert out["domain"] is None  # display-only ranking, never auto-picks
    assert [c["domain"] for c in out["candidates"]] == ["acme.com", "example-systems.it"]
    assert [c["score"] for c in out["candidates"]] == [3, 0]


def test_no_match_when_no_entity_has_a_url():
    entities = [_entity("Stripe", None), {"name": "Ghost", "types": []}]
    out = GkgDomainResolver(FakeClient(_ekg_ok(entities))).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_unconfigured_passthrough():
    client = FakeClient(
        {"backend": "ekg", "status": "unconfigured", "entities": [], "error": None}
    )
    out = GkgDomainResolver(client).discover("Stripe")
    assert out == {"status": "unconfigured", "domain": None, "candidates": []}


def test_error_passthrough_with_error_string():
    client = FakeClient(
        {"backend": "ekg", "status": "error", "entities": [], "error": "HTTP 401 from EKG Search"}
    )
    out = GkgDomainResolver(client).discover("Stripe")
    assert out["status"] == "error"
    assert out["domain"] is None
    assert out["candidates"] == []
    assert out["error"] == "HTTP 401 from EKG Search"


def test_root_domain_applied_subdomain_collapses_to_apex():
    entities = [
        _entity("Stripe", "https://www.stripe.com/en-ca", "c_a"),
        _entity("Acme", "https://dashboard.acme.io/about", "c_b"),
    ]
    out = pick_gkg_candidate(entities, "Stripe")
    assert out["status"] == "ambiguous"
    assert [c["domain"] for c in out["candidates"]] == ["stripe.com", "acme.io"]


def test_dedupe_same_domain_keeps_best_scoring_entity():
    entities = [
        _entity("Stripe Global", "https://stripe.com/", "c_weak"),  # +2 only
        _entity("Stripe", "https://payments.stripe.com/pricing", "c_strong"),  # +2 +1
    ]
    out = pick_gkg_candidate(entities, "Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert len(out["candidates"]) == 1
    best = out["candidates"][0]
    assert best["entity_id"] == "c_strong" and best["score"] == 3
    assert best["url"] == "https://payments.stripe.com/pricing"


def test_scoring_exact_name_and_token_overlap():
    """+2 name-vs-domain overlap; +1 only on an exact (casefold) name match."""
    entities = [
        _entity("Stripe", "https://stripe.com/", "c_exact"),
        _entity("Stripe Global", "https://stripe.io/", "c_overlap"),
        _entity("Stripe, Inc.", "https://stripe.net/", "c_legal"),  # +2 overlap, no +1
        _entity("Totally Different", "https://unrelated.org/", "c_none"),
    ]
    out = pick_gkg_candidate(entities, "Stripe")
    by_id = {c["entity_id"]: c["score"] for c in out["candidates"]}
    assert by_id["c_exact"] == 3
    assert by_id["c_overlap"] == 2
    assert by_id["c_legal"] == 2
    assert by_id["c_none"] == 0


def test_blank_name_short_circuits_without_client_call():
    client = FakeClient(_ekg_ok([_entity("Stripe", "https://stripe.com/")]))
    resolver = GkgDomainResolver(client)
    for blank in ("", "   "):
        assert resolver.discover(blank)["status"] == "no_match"
    assert client.calls == []


def test_broken_client_never_raises():
    client = FakeClient(error=RuntimeError("boom"))
    out = GkgDomainResolver(client).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_default_client_is_credential_gated_offline(monkeypatch):
    """client=None builds a real client; with no credentials the stage
    reports unconfigured instead of touching the network."""
    from src.identity.gkg_client import KnowledgeGraphClient

    for var in ("GOOGLE_APPLICATION_CREDENTIALS", "GKG_PROJECT_ID", "GOOGLE_KGSEARCH_KEY"):
        monkeypatch.delenv(var, raising=False)
    resolver = GkgDomainResolver()
    assert isinstance(resolver.client, KnowledgeGraphClient)
    assert resolver.discover("Stripe")["status"] == "unconfigured"


def test_registry_is_accepted_but_never_written():
    registry = FakeRegistry()
    client = FakeClient(_ekg_ok([_entity("Stripe", "https://stripe.com/")]))
    out = GkgDomainResolver(client, registry).discover("Stripe")
    assert out["status"] == "resolved"
    assert registry.upserts == []


def test_resolve_all_covers_every_name():
    client = FakeClient(_ekg_ok([_entity("Stripe", "https://stripe.com/")]))
    out = GkgDomainResolver(client).resolve_all(["Stripe", ""])
    assert set(out) == {"Stripe", ""}
    assert out["Stripe"]["status"] == "resolved"
    assert out[""]["status"] == "no_match"

"""Tests for the KnowledgeGraphClient (fake HTTP seam + fake token, no network)."""

from __future__ import annotations

import json
import sys

import pytest

import src.identity.gkg_client as gkg_client
from src.identity.gkg_client import (
    KGSEARCH_URL,
    KnowledgeGraphClient,
    parse_ekg_search,
    parse_kgsearch,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, content: bytes | None = None):
        self.status_code = status_code
        self.content = content if content is not None else json.dumps(payload or {}).encode()


class FakeHttp:
    """Injectable seam standing in for gkg_client._http_get (no network)."""

    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict | None, dict | None]] = []

    def __call__(self, url, *, headers=None, params=None, timeout=None):
        if self.error is not None:
            raise self.error
        self.calls.append((url, headers, params))
        return self.response


@pytest.fixture
def clean_gkg_env(monkeypatch):
    for var in ("GOOGLE_APPLICATION_CREDENTIALS", "GKG_PROJECT_ID", "GOOGLE_KGSEARCH_KEY"):
        monkeypatch.delenv(var, raising=False)


def _ekg_payload(elements: list[dict]) -> dict:
    return {"@context": "schema.org", "@type": "ItemList", "itemListElement": elements}


# ---------------------------------------------------------------------------
# credential gates
# ---------------------------------------------------------------------------


def test_ekg_unconfigured_without_token_fetcher_and_credentials(clean_gkg_env):
    client = KnowledgeGraphClient(backend="ekg")
    out = client.search("Stripe")
    assert out == {
        "backend": "ekg",
        "status": "unconfigured",
        "entities": [],
        "error": None,
    }


def test_kgsearch_unconfigured_without_legacy_key(clean_gkg_env):
    client = KnowledgeGraphClient(backend="kgsearch")
    out = client.search("Stripe")
    assert out["status"] == "unconfigured"
    assert out["backend"] == "kgsearch"


def test_env_defaults_for_project_and_key(monkeypatch, clean_gkg_env):
    monkeypatch.setenv("GKG_PROJECT_ID", "proj-from-env")
    monkeypatch.setenv("GOOGLE_KGSEARCH_KEY", "key-from-env")
    ekg = KnowledgeGraphClient()
    assert ekg.project_id == "proj-from-env"
    assert ekg.backend == "ekg"
    legacy = KnowledgeGraphClient(backend="kgsearch")
    assert legacy.legacy_key == "key-from-env"
    # explicit args win over env
    explicit = KnowledgeGraphClient(project_id="p", legacy_key="k")
    assert explicit.project_id == "p" and explicit.legacy_key == "k"


def test_unknown_backend_rejected():
    with pytest.raises(ValueError):
        KnowledgeGraphClient(backend="bogus")


def test_google_auth_absent_is_clean_unconfigured(monkeypatch, clean_gkg_env):
    """GOOGLE_APPLICATION_CREDENTIALS set but google-auth missing -> unconfigured.

    Simulated by halting the google* imports at sys.modules (deterministic
    whether or not the optional [gkg] extra is installed); must not crash.
    """
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "C:/fake/sa.json")
    monkeypatch.setitem(sys.modules, "google", None)
    monkeypatch.setitem(sys.modules, "google.oauth2", None)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", None)
    client = KnowledgeGraphClient(backend="ekg")
    out = client.search("Stripe")
    assert out["status"] == "unconfigured"
    assert out["error"] is None


# ---------------------------------------------------------------------------
# EKG backend (ok paths)
# ---------------------------------------------------------------------------


def test_ekg_ok_result_wrapped_shape(monkeypatch, clean_gkg_env):
    payload = _ekg_payload(
        [
            {
                "result": {
                    "id": "c_stripe",
                    "name": "Stripe",
                    "url": "https://stripe.com/",
                    "@type": ["Organization", "Corporation"],
                }
            }
        ]
    )
    fake = FakeHttp(FakeResponse(payload=payload))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "tok-1", project_id="acme-prod")
    out = client.search("Stripe")

    assert out["backend"] == "ekg" and out["status"] == "ok" and out["error"] is None
    assert out["entities"] == [
        {"name": "Stripe", "url": "https://stripe.com/", "entity_id": "c_stripe", "types": ["Organization", "Corporation"]}
    ]
    # One unpaced Search call: bearer auth, project path, pinned query params.
    assert len(fake.calls) == 1
    url, headers, params = fake.calls[0]
    assert url == (
        "https://enterpriseknowledgegraph.googleapis.com/v1/projects/"
        "acme-prod/locations/global/publicKnowledgeGraphEntities:Search"
    )
    assert headers["Authorization"] == "Bearer tok-1"
    assert params["query"] == "Stripe"
    assert params["types"] == ["Organization"]
    assert params["languages"] == ["en"]
    assert params["limit"] == 5


def test_ekg_ok_bare_and_item_nesting_shapes(monkeypatch, clean_gkg_env):
    """Tolerant nesting: entities may sit directly in itemListElement, under
    "result" or under "item" — all three parse; nothing usable is skipped."""
    payload = _ekg_payload(
        [
            {"id": "c_a", "name": "Acme", "url": "https://acme.io/"},
            {"item": {"id": "c_b", "name": "Beta", "url": "https://beta.dev/"}},
            {"result": {"id": "c_c", "name": "Gamma", "url": "https://gamma.co/"}},
            {"unrelated": "noise"},
        ]
    )
    fake = FakeHttp(FakeResponse(payload=payload))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    out = client.search("Acme")
    assert out["status"] == "ok"
    assert [e["entity_id"] for e in out["entities"]] == ["c_a", "c_b", "c_c"]


def test_ekg_entity_id_googlekgmid_identifier_rule(monkeypatch, clean_gkg_env):
    """Cloud KG MID via "id"; legacy /m/ only under identifier[].propertyID
    == "googleKgMID"; mid-ish fields elsewhere are never guessed into ids."""
    payload = _ekg_payload(
        [
            {
                "name": "Legacy Co",
                "url": "https://legacy.example/",
                "identifier": [{"propertyID": "googleKgMID", "value": "/m/0abc"}],
            },
            {"name": "Decoy Co", "mid": "/m/0xyz", "url": "https://decoy.example/"},
        ]
    )
    entities = parse_ekg_search(payload)
    assert entities[0]["entity_id"] == "/m/0abc"
    assert entities[1]["entity_id"] is None  # bare "mid" field is not an id


def test_ekg_entity_without_any_derived_field_is_skipped():
    payload = _ekg_payload([{"description": "a thing"}, {"result": {"k": 1}}])
    assert parse_ekg_search(payload) == []
    assert parse_ekg_search({}) == []
    assert parse_ekg_search({"itemListElement": "nope"}) == []


def test_ekg_missing_project_id_is_error_not_crash(monkeypatch, clean_gkg_env):
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id=None)
    out = client.search("Stripe")
    assert out["status"] == "error"
    assert "GKG_PROJECT_ID" in out["error"]


# ---------------------------------------------------------------------------
# legacy kgsearch backend
# ---------------------------------------------------------------------------


def test_kgsearch_ok_and_absent_url_entity_skipped(monkeypatch, clean_gkg_env):
    """result.url present -> parsed; absent/empty url -> entity skipped;
    resultScore is read nowhere and never lands in the output."""
    payload = {
        "itemListElement": [
            {
                "result": {
                    "@type": "Organization",
                    "name": "Stripe",
                    "url": "https://stripe.com/",
                    "@id": "kg:/m/0f8l9c",
                },
                "resultScore": 56.5,
            },
            {"result": {"@type": "Organization", "name": "Mystery"}, "resultScore": 12.0},
            {"result": {"name": "EmptyUrl", "url": ""}},
        ]
    }
    fake = FakeHttp(FakeResponse(payload=payload))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(backend="kgsearch", legacy_key="key-123")
    out = client.search("Stripe")

    assert out == {
        "backend": "kgsearch",
        "status": "ok",
        "entities": [
            {
                "name": "Stripe",
                "url": "https://stripe.com/",
                "entity_id": "kg:/m/0f8l9c",
                "types": ["Organization"],
            }
        ],
        "error": None,
    }
    url, _headers, params = fake.calls[0]
    assert url == KGSEARCH_URL
    assert params["key"] == "key-123"
    assert params["query"] == "Stripe"
    assert params["types"] == "Organization"
    assert params["languages"] == "en"
    assert params["limit"] == 5


def test_parse_kgsearch_skips_non_result_elements():
    payload = {"itemListElement": ["junk", {"no_result": 1}, {"result": "not-a-dict"}]}
    assert parse_kgsearch(payload) == []
    assert parse_kgsearch({}) == []


def test_backend_selection_honored(monkeypatch, clean_gkg_env):
    """Same query, two clients: the configured backend picks the endpoint."""
    fake = FakeHttp(FakeResponse(payload={}))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    ekg = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    assert ekg.search("X")["backend"] == "ekg"
    assert fake.calls[-1][0].startswith("https://enterpriseknowledgegraph.googleapis.com/")
    legacy = KnowledgeGraphClient(backend="kgsearch", legacy_key="k")
    assert legacy.search("X")["backend"] == "kgsearch"
    assert fake.calls[-1][0] == KGSEARCH_URL


# ---------------------------------------------------------------------------
# error paths (never raise) + ToS shape
# ---------------------------------------------------------------------------


def test_http_error_status_is_error(monkeypatch, clean_gkg_env):
    fake = FakeHttp(FakeResponse(status_code=401, payload={"error": {"code": 401}}))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    out = client.search("Stripe")
    assert out["status"] == "error"
    assert "401" in out["error"]
    assert out["entities"] == []


def test_seam_exception_is_error(monkeypatch, clean_gkg_env):
    fake = FakeHttp(error=ConnectionError("boom"))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    out = client.search("Stripe")
    assert out["status"] == "error"
    assert "ConnectionError" in out["error"]


def test_token_fetcher_raising_is_error(clean_gkg_env):
    def broken_fetcher():
        raise RuntimeError("oauth down")

    client = KnowledgeGraphClient(token_fetcher=broken_fetcher, project_id="p")
    out = client.search("Stripe")
    assert out["status"] == "error"
    assert "oauth down" in out["error"]


def test_non_json_body_is_error(monkeypatch, clean_gkg_env):
    fake = FakeHttp(FakeResponse(status_code=200, content=b"<html>gateway</html>"))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    out = client.search("Stripe")
    assert out["status"] == "error"
    assert "non-JSON" in out["error"]


def test_blank_name_short_circuits_without_http(monkeypatch, clean_gkg_env):
    fake = FakeHttp(FakeResponse(payload={}))
    monkeypatch.setattr(gkg_client, "_http_get", fake)
    client = KnowledgeGraphClient(token_fetcher=lambda: "t", project_id="p")
    for blank in ("", "   "):
        out = client.search(blank)
        assert out["status"] == "ok"
        assert out["entities"] == []
    assert fake.calls == []


def test_entities_carry_only_derived_fields(clean_gkg_env):
    """ToS: raw payloads never cross the client boundary — derived fields only."""
    raw_entity = {
        "id": "c_x",
        "name": "Stripe",
        "url": "https://stripe.com/",
        "description": "raw-licensed text that must not leak",
        "detailedDescription": {"articleBody": "leak-me"},
    }
    for entity in parse_ekg_search(_ekg_payload([{"result": raw_entity}])):
        assert set(entity) <= set(gkg_client.ENTITY_KEYS)
        assert "description" not in json.dumps(entity)

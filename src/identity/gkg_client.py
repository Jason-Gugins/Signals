"""Thin Google Knowledge Graph client with two credential-gated backends.

Waterfall stage 3 of the Clay "find the company" clone (plan T3b). Two backends:

- ``ekg`` (default): Cloud Enterprise Knowledge Graph
  ``projects/{project}/locations/global/publicKnowledgeGraphEntities:Search``.
  Auth is an OAuth2 bearer token from a Google service account
  (cloud-platform scope) — bare API keys do NOT work. The entity-level
  response schema is UNDOCUMENTED (T1 probe: only the top level
  ``{"@context", "@type": "ItemList", "itemListElement": [...]}`` is pinned),
  so parsing is defensive and extracts only what is actually present.
- ``kgsearch`` (legacy, off by default): ``kgsearch.googleapis.com/v1/entities:search``
  with an API key. ``itemListElement[].result.url`` is the official website
  (often absent for small companies). ``resultScore`` exists there but is
  deliberately ignored — never load-bearing logic (plan: "resultScore logic
  deleted, not ported").

Google ToS §5.e.1 / EKG per-field licensing: raw payloads are never persisted
or returned — only derived fields (name, url, entity id, types). Entity ids
are Cloud KG MIDs (``c-...`` format); legacy ``/m/...`` MIDs count only under
``identifier[].propertyID == "googleKgMID"`` — never scraped from other fields.

Network seam: ``_http_get`` is a module-level deferred-import wrapper around
httpx.get (mirrors curl_fetcher.curl_cffi_get) — the module imports cleanly
and tests patch the seam to inject canned responses, so the offline lane
never touches httpx or the network. Token fetching is injectable too
(``token_fetcher``), so tests never need google-auth or a token endpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

EKG_SEARCH_URL = (
    "https://enterpriseknowledgegraph.googleapis.com/v1/projects/"
    "{project}/locations/global/publicKnowledgeGraphEntities:Search"
)
KGSEARCH_URL = "https://kgsearch.googleapis.com/v1/entities:search"

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# One call = one candidate set: EKG Search has no pagination.
EKG_LIMIT = 5
KGSEARCH_LIMIT = 5

REQUEST_TIMEOUT_S = 30.0

# Derived entity dict — the ONLY shape this client ever returns (Google ToS:
# no raw payloads cross this boundary).
ENTITY_KEYS = ("name", "url", "entity_id", "types")


def _http_get(url: str, *, headers=None, params=None, timeout: float = REQUEST_TIMEOUT_S):
    """Thin deferred-import wrapper around httpx.get.

    The import is deferred to call time so that this module imports even when
    httpx is unavailable (e.g. mocked test runs). Tests patch this callable to
    inject canned responses (curl_fetcher.curl_cffi_get pattern).
    """
    import httpx

    return httpx.get(url, headers=headers, params=params, timeout=timeout)


def _build_service_account_token_fetcher(creds_path: str):
    """Build a ``() -> str`` bearer-token callable from a service-account file.

    google-auth (the optional ``[gkg]`` extra) is imported lazily at call
    time — ImportError propagates so the caller can downgrade to
    "unconfigured" instead of crashing.
    """
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as AuthRequest

    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=[CLOUD_PLATFORM_SCOPE]
    )

    def fetch() -> str:
        if not creds.valid:
            creds.refresh(AuthRequest())
        return creds.token

    return fetch


@dataclass
class _HttpCall:
    """Record of one seam call (test introspection + error messages)."""

    url: str
    headers: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)


def _stringish(value) -> str | None:
    """First usable string from a tolerantly-shaped JSON-LD field (pure)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    if isinstance(value, dict):  # e.g. {"@value": "..."} JSON-LD literal
        v = value.get("@value")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _type_list(value) -> list[str]:
    """schema.org / JSON-LD @type field -> list of non-empty strings (pure)."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def _entity_id(entity: dict) -> str | None:
    """Entity id from "id", else identifier[].propertyID == "googleKgMID" (pure).

    Cloud KG MIDs arrive as "id" (``c-...`` format). Legacy ``/m/...`` MIDs are
    only honored under identifier[].propertyID == "googleKgMID" — a bare
    "mid"-ish field elsewhere is never guessed into an id.
    """
    eid = entity.get("id")
    if isinstance(eid, str) and eid.strip():
        return eid.strip()
    identifier = entity.get("identifier")
    if isinstance(identifier, list):
        for ident in identifier:
            if not isinstance(ident, dict):
                continue
            if str(ident.get("propertyID") or "") == "googleKgMID":
                val = ident.get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _unwrap_element(element) -> dict | None:
    """itemListElement -> entity dict under "result"/"item"/directly (pure)."""
    if not isinstance(element, dict):
        return None
    for key in ("result", "item"):
        nested = element.get(key)
        if isinstance(nested, dict):
            return nested
    return element


def _parse_ekg_entity(entity) -> dict | None:
    """One EKG entity -> derived dict, or None when nothing usable is present.

    The entity-level schema is undocumented, so nothing beyond
    name/url/entity-id/@type is read and absent fields stay None (never
    guessed). Entities with none of the four derived fields are skipped.
    """
    if not isinstance(entity, dict):
        return None
    name = _stringish(entity.get("name"))
    url = _stringish(entity.get("url"))
    eid = _entity_id(entity)
    types = _type_list(entity.get("@type"))
    if name is None and url is None and eid is None and not types:
        return None
    return {"name": name, "url": url, "entity_id": eid, "types": types}


def parse_ekg_search(raw: dict) -> list[dict]:
    """ItemList payload -> derived entity dicts (pure). Never persists raw."""
    elements = raw.get("itemListElement")
    if not isinstance(elements, list):
        return []
    out: list[dict] = []
    for element in elements:
        entity = _parse_ekg_entity(_unwrap_element(element))
        if entity is not None:
            out.append(entity)
    return out


def parse_kgsearch(raw: dict) -> list[dict]:
    """Legacy kgsearch payload -> derived entity dicts (pure).

    Only entities with a non-empty ``result.url`` survive: url is the sole
    signal this backend contributes to the waterfall. ``resultScore`` is
    read nowhere.
    """
    elements = raw.get("itemListElement")
    if not isinstance(elements, list):
        return []
    out: list[dict] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        result = element.get("result")
        if not isinstance(result, dict):
            continue
        url = _stringish(result.get("url"))
        if not url:
            continue
        name = _stringish(result.get("name"))
        eid = _stringish(result.get("@id"))
        out.append(
            {"name": name, "url": url, "entity_id": eid, "types": _type_list(result.get("@type"))}
        )
    return out


class KnowledgeGraphClient:
    """Search Google's Knowledge Graphs; returns derived entities only.

    ``token_fetcher`` is an injectable ``() -> str`` bearer-token callable so
    the offline test lane never needs google-auth or the network. Without it,
    a token fetcher is built lazily from GOOGLE_APPLICATION_CREDENTIALS via
    google-auth (deferred import; missing library -> clean unconfigured).
    """

    def __init__(
        self,
        token_fetcher=None,
        project_id: str | None = None,
        legacy_key: str | None = None,
        backend: str = "ekg",
    ):
        if backend not in ("ekg", "kgsearch"):
            raise ValueError(f"unknown GKG backend: {backend!r} (expected 'ekg' or 'kgsearch')")
        self.token_fetcher = token_fetcher
        self.project_id = project_id or os.environ.get("GKG_PROJECT_ID") or None
        self.legacy_key = legacy_key or os.environ.get("GOOGLE_KGSEARCH_KEY") or None
        self.backend = backend
        self._sa_token_fetcher = None  # lazily built service-account token fetcher
        self.last_call: _HttpCall | None = None

    # -- status helpers ----------------------------------------------------

    def _unconfigured(self) -> dict:
        return {"backend": self.backend, "status": "unconfigured", "entities": [], "error": None}

    def _error(self, message: str) -> dict:
        return {"backend": self.backend, "status": "error", "entities": [], "error": message}

    # -- entry point -------------------------------------------------------

    def search(self, name: str) -> dict:
        """Name -> {"backend", "status", "entities", "error"}. Never raises."""
        try:
            return self._search(name)
        except Exception as exc:  # token/HTTP/parse failures never raise
            return self._error(f"{type(exc).__name__}: {exc}")

    def _search(self, name: str) -> dict:
        term = (name or "").strip()
        if not term:
            return {"backend": self.backend, "status": "ok", "entities": [], "error": None}
        if self.backend == "kgsearch":
            return self._search_kgsearch(term)
        return self._search_ekg(term)

    # -- EKG backend -------------------------------------------------------

    def _ekg_token(self) -> str:
        """Bearer token; returns "" when the credential gate can't be satisfied.

        Raises ImportError when google-auth is not installed (downgraded to
        unconfigured by the caller); other failures propagate to search() as
        status "error".
        """
        if self.token_fetcher is not None:
            return str(self.token_fetcher())
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_path:
            return ""
        if self._sa_token_fetcher is None:
            self._sa_token_fetcher = _build_service_account_token_fetcher(creds_path)
        return str(self._sa_token_fetcher())

    def _search_ekg(self, term: str) -> dict:
        try:
            token = self._ekg_token()
        except ImportError:
            # Optional [gkg] extra not installed — clean unconfigured, no crash.
            return self._unconfigured()
        if not token:
            return self._unconfigured()
        if not self.project_id:
            return self._error("GKG project id missing (set GKG_PROJECT_ID)")
        url = EKG_SEARCH_URL.format(project=self.project_id)
        call = _HttpCall(
            url=url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "query": term,
                "types": ["Organization"],
                "languages": ["en"],
                "limit": EKG_LIMIT,
            },
        )
        self.last_call = call
        resp = _http_get(url, headers=call.headers, params=call.params)
        if getattr(resp, "status_code", 0) != 200:
            return self._error(f"HTTP {getattr(resp, 'status_code', '?')} from EKG Search")
        try:
            raw = json.loads(resp.content)
        except (ValueError, TypeError) as exc:
            return self._error(f"EKG returned non-JSON body: {exc}")
        if not isinstance(raw, dict):
            return self._error("EKG returned unexpected JSON top level")
        return {
            "backend": "ekg",
            "status": "ok",
            "entities": parse_ekg_search(raw),
            "error": None,
        }

    # -- legacy kgsearch backend -------------------------------------------

    def _search_kgsearch(self, term: str) -> dict:
        if not self.legacy_key:
            return self._unconfigured()
        call = _HttpCall(
            url=KGSEARCH_URL,
            headers={},
            params={
                "query": term,
                "types": "Organization",
                "languages": "en",
                "limit": KGSEARCH_LIMIT,
                "key": self.legacy_key,
            },
        )
        self.last_call = call
        resp = _http_get(url=call.url, params=call.params)
        if getattr(resp, "status_code", 0) != 200:
            return self._error(f"HTTP {getattr(resp, 'status_code', '?')} from kgsearch")
        try:
            raw = json.loads(resp.content)
        except (ValueError, TypeError) as exc:
            return self._error(f"kgsearch returned non-JSON body: {exc}")
        if not isinstance(raw, dict):
            return self._error("kgsearch returned unexpected JSON top level")
        return {
            "backend": "kgsearch",
            "status": "ok",
            "entities": parse_kgsearch(raw),
            "error": None,
        }

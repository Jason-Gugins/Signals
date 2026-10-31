"""Tests for Wikidata P856 domain discovery (fake fetcher, no network)."""

from __future__ import annotations

import json

from src.identity.wikidata_ids import (
    WikidataDomainResolver,
    _is_business,
    build_claims_url,
    build_search_url,
    parse_claims,
    parse_wbsearch,
    pick_wikidata_candidate,
)


class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None


class FakeFetcher:
    """Routes canned T1-probe-shaped payloads by URL substring (no network)."""

    def __init__(
        self,
        routes: dict[str, bytes] | None = None,
        ok: bool = True,
        error: Exception | None = None,
    ):
        self.routes = routes or {}
        self.ok = ok
        self.error = error
        self.tasks: list = []

    def get(self, task):
        if self.error is not None:
            raise self.error
        self.tasks.append(task)
        for key, body in self.routes.items():
            if key in task.url:
                return FakeResult(body, ok=self.ok)
        return FakeResult(None, ok=False)


class FakeRegistry:
    def __init__(self):
        self.upserts: list = []

    def upsert(self, account, *, source=None):
        self.upserts.append((account, source))
        return account


def _search_payload(entities: list[dict]) -> bytes:
    return json.dumps({"searchinfo": {"search": "x"}, "search": entities}).encode()


def _entity(qid: str, label: str, description: str = "") -> dict:
    return {"id": qid, "label": label, "description": description}


def _claims_payload(urls: list[str]) -> bytes:
    return json.dumps(
        {
            "claims": {
                "P856": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "property": "P856",
                            "datatype": "url",
                            "datavalue": {"value": url, "type": "string"},
                        },
                        "type": "statement",
                        "rank": "normal",
                    }
                    for url in urls
                ]
            }
        }
    ).encode()


# T1 probe (data/probe/KEYLESS_IDENTITY_2026_09.md): the real "Stripe" top-5.
_T1_SEARCH = [
    _entity("Q7624104", "Stripe", "Irish-American payment technology company"),
    _entity("Q3421342", "stripe", "long, narrow band of color, often in alternating sets"),
    _entity("Q127900502", "Stripe", "fictional character in the Gremlins franchise"),
    _entity("Q117454382", "Stripe", "South African progamer"),
    _entity("Q297115", "Meloidae", "family of beetles"),
]


def _t1_routes() -> dict[str, bytes]:
    return {
        "wbsearchentities": _search_payload(_T1_SEARCH),
        "entity=Q7624104": _claims_payload(["https://stripe.com/"]),
    }


def test_resolved_single_business_entity_with_p856():
    fetcher = FakeFetcher(
        {
            "wbsearchentities": _search_payload(
                [_entity("Q7624104", "Stripe", "Irish-American payment technology company")]
            ),
            "entity=Q7624104": _claims_payload(["https://stripe.com/"]),
        }
    )
    registry = FakeRegistry()
    out = WikidataDomainResolver(fetcher, registry).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert len(out["candidates"]) == 1
    cand = out["candidates"][0]
    assert cand == {
        "qid": "Q7624104",
        "label": "Stripe",
        "description": "Irish-American payment technology company",
        "domain": "stripe.com",
        "p856_url": "https://stripe.com/",
        "score": 5,
    }
    # 2-stage flow: search + one claims call. Never auto-persists.
    assert len(fetcher.tasks) == 2
    assert "wbsearchentities" in fetcher.tasks[0].url
    assert "wbgetclaims" in fetcher.tasks[1].url
    assert registry.upserts == []


def test_ambiguous_two_business_entities_ranks_but_never_picks():
    fetcher = FakeFetcher(
        {
            "wbsearchentities": _search_payload(
                [
                    _entity("Q1", "Acme Corporation", "American software company"),
                    _entity("Q2", "Acme Systems", "Italian technology company"),
                ]
            ),
            "entity=Q1": _claims_payload(["https://acme.com/"]),
            "entity=Q2": _claims_payload(["https://example-systems.it/"]),
        }
    )
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Acme")
    assert out["status"] == "ambiguous"
    assert out["domain"] is None
    assert [c["qid"] for c in out["candidates"]] == ["Q1", "Q2"]
    assert [c["score"] for c in out["candidates"]] == [4, 2]
    assert [c["domain"] for c in out["candidates"]] == ["acme.com", "example-systems.it"]


def test_no_business_entities_returns_no_match():
    fetcher = FakeFetcher(
        {
            "wbsearchentities": _search_payload(
                [
                    _entity("Q297115", "Meloidae", "family of beetles"),
                    _entity("Q3421342", "stripe", "long, narrow band of color"),
                ]
            )
        }
    )
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_description_keyed_filter_excludes_beetle_and_character():
    fetcher = FakeFetcher(_t1_routes())
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    assert [c["qid"] for c in out["candidates"]] == ["Q7624104"]
    # 1 search + claims for each of the capped 5 entities.
    assert len(fetcher.tasks) == 6


def test_filter_does_real_work_generic_name_resolves_to_business_only():
    """Plan T5 test 2 — the _is_business filter earning its keep on a
    generic name: the "Acme" search returns one business entity
    (P856-bearing) and one non-business hit (fictional-character
    description, no P856) -> the non-business hit is filtered out entirely
    and the business one resolves."""
    fetcher = FakeFetcher(
        {
            "wbsearchentities": _search_payload(
                [
                    _entity("Q1", "Acme Corporation", "American software company"),
                    _entity("Q2", "Acme", "fictional character in the Road Runner cartoons"),
                ]
            ),
            "entity=Q1": _claims_payload(["https://acme.com/"]),
            "entity=Q2": _claims_payload([]),
        }
    )
    registry = FakeRegistry()
    out = WikidataDomainResolver(fetcher, registry).discover("Acme")
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.com"
    # The non-business entity is gone, not merely unranked.
    assert [c["qid"] for c in out["candidates"]] == ["Q1"]
    assert out["candidates"][0]["score"] == 4
    # 1 search + claims for each of the 2 entities. Never auto-persists.
    assert len(fetcher.tasks) == 3
    assert registry.upserts == []


def test_p856_subdomain_resolves_to_root_domain():
    fetcher = FakeFetcher(
        {
            "wbsearchentities": _search_payload(
                [_entity("Q7624104", "Stripe", "Irish-American payment technology company")]
            ),
            "entity=Q7624104": _claims_payload(["https://www.stripe.com/en-ca"]),
        }
    )
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Stripe")
    assert out["status"] == "resolved"
    assert out["domain"] == "stripe.com"
    # Raw P856 kept as evidence; domain is the registrable root.
    assert out["candidates"][0]["p856_url"] == "https://www.stripe.com/en-ca"


def test_search_url_encodes_name():
    url = build_search_url("Acme & Co")
    assert url.startswith("https://www.wikidata.org/w/api.php?")
    assert "action=wbsearchentities" in url
    assert "search=Acme+%26+Co" in url
    assert "language=en" in url
    assert "type=item" in url
    assert "format=json" in url
    assert "limit=5" in url
    claims_url = build_claims_url("Q7624104")
    assert "action=wbgetclaims" in claims_url
    assert "entity=Q7624104" in claims_url
    assert "property=P856" in claims_url


def test_discover_encodes_name_with_space_and_ampersand():
    fetcher = FakeFetcher({"wbsearchentities": _search_payload([])})
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Acme & Co")
    assert out["status"] == "no_match"
    assert "search=Acme+%26+Co" in fetcher.tasks[0].url


def test_parse_wbsearch_roundtrip():
    entities = parse_wbsearch(_search_payload(_T1_SEARCH))
    assert [e["id"] for e in entities] == [
        "Q7624104",
        "Q3421342",
        "Q127900502",
        "Q117454382",
        "Q297115",
    ]
    assert entities[0]["label"] == "Stripe"
    assert parse_wbsearch(b"{}") == []


def test_parse_claims_plain_and_nested_shapes():
    # Real T1 probe artifact shape: datavalue.value is the bare URL string.
    assert parse_claims(_claims_payload(["https://stripe.com/"])) == [
        "https://stripe.com/"
    ]
    # Nested datavalue.value["value"] shape is unwrapped when it appears.
    nested = json.dumps(
        {
            "claims": {
                "P856": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "value": {"value": "https://acme.com/", "type": "string"},
                                "type": "string",
                            }
                        }
                    }
                ]
            }
        }
    ).encode()
    assert parse_claims(nested) == ["https://acme.com/"]
    # snaktype=somevalue (no datavalue) and empty payloads yield nothing.
    somevalue = json.dumps(
        {"claims": {"P856": [{"mainsnak": {"snaktype": "somevalue"}}]}}
    ).encode()
    assert parse_claims(somevalue) == []
    assert parse_claims(b"{}") == []


def test_pick_wikidata_candidate_decides_on_domain_bearing_survivors():
    entities = [
        {"qid": "Q1", "label": "Acme Corporation", "description": "software company", "p856_url": None},
        {"qid": "Q2", "label": "Acme Systems", "description": "technology company", "p856_url": "https://acme.io/"},
    ]
    # One business survivor without P856 cannot block the domain-bearing one.
    out = pick_wikidata_candidate(entities, "Acme")
    assert out["status"] == "resolved"
    assert out["domain"] == "acme.io"
    assert [c["qid"] for c in out["candidates"]] == ["Q2", "Q1"]


def test_business_terms_substring_false_survivor_is_pinned():
    """Plan T5 test 3 — CHARACTERIZATION: pin the _is_business substring
    false-survivor risk. "software" is a casefold substring term in
    BUSINESS_TERMS (src/identity/wikidata_ids.py:38-56), so a non-company
    OSS *tool* described "software for signal processing" passes the
    business filter exactly like a software company would. Deliberately NOT
    fixed this wave: the decision rule bounds the damage (a false survivor
    without a domain cannot resolve, and one with a domain resolves only
    when it is the ONLY domain-bearing survivor — otherwise everything goes
    to the ambiguous review queue). Recorded risk, per the plan."""
    description = "free and open-source software for signal processing"
    # The substring mechanics: a non-company described this way is
    # business-flagged purely because "software" occurs in the text.
    assert _is_business({"label": "Signal DSP", "description": description}) is True

    entity = {
        "qid": "Q999",
        "label": "Signal DSP",
        "description": description,
        "p856_url": "https://signaldsp.org/",
    }
    out = pick_wikidata_candidate([entity], "Signal DSP")
    # P856-bearing false survivor, alone with a domain -> resolves. That is
    # the pinned risk: the filter alone cannot keep a non-company out.
    assert out["status"] == "resolved"
    assert out["domain"] == "signaldsp.org"
    assert out["candidates"][0]["qid"] == "Q999"
    assert out["candidates"][0]["score"] == 5  # P856 + two-way + exact label


def test_fetch_error_returns_no_match_without_raising():
    fetcher = FakeFetcher(error=ConnectionError("boom"))
    out = WikidataDomainResolver(fetcher, FakeRegistry()).discover("Stripe")
    assert out == {"status": "no_match", "domain": None, "candidates": []}


def test_blank_name_short_circuits_without_fetch():
    fetcher = FakeFetcher(_t1_routes())
    resolver = WikidataDomainResolver(fetcher, FakeRegistry())
    assert resolver.discover("")["status"] == "no_match"
    assert resolver.discover("   ")["status"] == "no_match"
    assert fetcher.tasks == []


def test_resolve_all_covers_every_name():
    fetcher = FakeFetcher(_t1_routes())
    out = WikidataDomainResolver(fetcher, FakeRegistry()).resolve_all(["Stripe", ""])
    assert out["Stripe"]["status"] == "resolved"
    assert out[""]["status"] == "no_match"
    assert set(out) == {"Stripe", ""}

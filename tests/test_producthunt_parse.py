"""Task 10 — Product Hunt parser/adapter tests (synthetic fixture only).

Per the P2 spike (data/probe/P2_SOURCE_SPIKE.md), live producthunt.com
fetching is CF-blocked (Cloudflare managed challenge on the first request);
every test here runs against the synthetic fixture only.
"""

from datetime import date
from pathlib import Path

import pytest

from src.core.models import Account, Document
from src.sources.base import FetchTask
from src.sources.content.producthunt import (
    ProductHuntSource,
    launches_to_candidates,
    parse_producthunt_launches,
    ph_slug,
    producthunt_url,
)

FIXTURE = Path("tests/fixtures/content/producthunt_launches.html")
SYNTHETIC_HEADER = "synthetic — live capture blocked (P2 spike 2026-08-31, see data/probe/P2_SOURCE_SPIKE.md)"
TODAY = date(2026, 8, 31)


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def items(html) -> list[dict]:
    return parse_producthunt_launches(html)


# ---------------------------------------------------------------- parse ----


def test_fixture_carries_synthetic_header():
    assert SYNTHETIC_HEADER in FIXTURE.read_text(encoding="utf-8")


def test_parse_count(items):
    assert len(items) == 3


def test_parse_fields(items):
    by_slug = {it["slug"]: it for it in items}
    assert set(by_slug) == {"acme-launcher", "zapforge", "quillpad"}
    acme = by_slug["acme-launcher"]
    assert acme["name"] == "Acme Launcher"
    assert acme["website"].startswith("https://acme.com")
    assert acme["votes"] == 248
    assert "Acme workspaces" in acme["tagline"]
    assert by_slug["zapforge"]["votes"] == 96
    assert by_slug["quillpad"]["tagline"] == "Markdown notes that link themselves."


def test_parse_empty_and_garbage():
    assert parse_producthunt_launches("") == []
    assert parse_producthunt_launches("<html><body>Just a moment...</body></html>") == []


# ----------------------------------------------------------- candidates ----


def test_candidates_both_types(items):
    acct = Account(domain="acme.com", name="Acme")
    out = launches_to_candidates(items, acct, today=TODAY)
    types = {c.natural_key: c.signal_type for c in out}
    assert types == {"ph:acme-launcher": "product_launch", "phalt:zapforge": "intent_3rd_topic"}

    launch = next(c for c in out if c.natural_key == "ph:acme-launcher")
    assert launch.observed_at == "2026-08-31"
    assert launch.url == "https://www.producthunt.com/products/acme-launcher"
    assert launch.title == "Acme Launcher"
    assert launch.confidence > 0.6

    intent = next(c for c in out if c.natural_key == "phalt:zapforge")
    assert intent.confidence < launch.confidence
    assert "Acme" in (intent.evidence_data.get("tagline") or "")


def test_candidates_domain_only_match(items):
    # Account with no name but matching website domain still yields product_launch.
    acct = Account(domain="acme.com", name=None)
    out = launches_to_candidates(items, acct, today=TODAY)
    assert [c.natural_key for c in out] == ["ph:acme-launcher"]


def test_candidates_no_match(items):
    acct = Account(domain="unrelated.io", name="Unrelated")
    assert launches_to_candidates(items, acct, today=TODAY) == []


# ------------------------------------------------------------- adapter ----


def test_plan_builds_product_url():
    task = ProductHuntSource().plan(Account(domain="acme.com", name="Acme"), None)
    assert task == [
        FetchTask(source="content_producthunt", url="https://www.producthunt.com/products/acme", domain="acme.com")
    ]
    assert ProductHuntSource().key == "content_producthunt"
    assert ProductHuntSource().tier == "http"
    assert ProductHuntSource().cadence_hours == 168


def test_plan_parse_round_trip():
    """plan() URL slug and the fixture's /products/ hrefs agree on the scheme."""
    acct = Account(domain="acme.com", name="Acme")
    src = ProductHuntSource()
    task = src.plan(acct, None)[0]
    assert producthunt_url(ph_slug(acct.name)) == task.url
    doc = Document(doc_id="ph1", source="content_producthunt", body=FIXTURE.read_bytes())
    out = src.parse(doc, acct, {"today": TODAY.isoformat()})
    assert {c.natural_key for c in out} == {"ph:acme-launcher", "phalt:zapforge"}
    assert all(c.url.startswith(task.url.rsplit("/", 1)[0] + "/") for c in out)


def test_parse_empty_body():
    assert ProductHuntSource().parse(Document(doc_id="e", source="content_producthunt", body=None), Account(domain="x.com"), {"today": "2026-08-31"}) == []


def test_adapter_docstring_states_cf_block():
    import inspect

    from src.sources.content import producthunt as mod

    assert "CF-blocked" in inspect.getdoc(mod.ProductHuntSource)
    assert "Cloudflare" in inspect.getdoc(mod)
    assert "data/probe/P2_SOURCE_SPIKE.md" in inspect.getdoc(mod)

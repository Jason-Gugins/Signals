"""Tests for the YC Inertia data-page parser (Task 8, STUB per spike)."""

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.yc.collector import YC_BATCH_URL, YcBatchSource, parse_yc_batch, yc_to_candidates

FIXTURE = Path("tests/fixtures/yc/yc_batch_inertia.html")
REAL_SHAPE = (
    '<body class="ycdc2 companies index">'
    '<div data-page="{&quot;component&quot;:&quot;ycdc_new/pages/Companies/IndexPage&quot;,'
    '&quot;props&quot;:{&quot;env&quot;:&quot;production&quot;,&quot;currentBatch&quot;:&quot;Summer 2026&quot;},'
    '&quot;url&quot;:&quot;/companies?batch=S24&quot;,&quot;version&quot;:&quot;9f2c1a&quot;}"></div>'
    "</body>"
)


def test_parse_extracts_three_companies_from_enriched_fixture():
    companies = parse_yc_batch(FIXTURE.read_text(encoding="utf-8"))
    assert len(companies) == 3
    slugs = [c["slug"] for c in companies]
    assert slugs == ["acme-rockets", "acme-materials", "other-startup"]
    assert all(c["batch"] == "S24" for c in companies)


def test_real_spike_shape_no_companies_yields_empty_without_error():
    # Props carry only env/currentBatch — the actual shape per the spike.
    assert parse_yc_batch(REAL_SHAPE) == []
    # Degenerate inputs also degrade to [] rather than raising.
    assert parse_yc_batch("<html><body>no data-page here</body></html>") == []
    assert parse_yc_batch('<div data-page="not json at all"></div>') == []


def test_yc_to_candidates_identity_evidence():
    acct = Account(domain="acmerockets.example", name="Acme Rockets")
    today = date(2026, 8, 31)
    companies = parse_yc_batch(FIXTURE.read_text(encoding="utf-8"))
    cands = yc_to_candidates(companies, acct, today=today)
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "intent_3rd_topic"
    assert c.natural_key == "yc:acme-rockets"
    assert c.observed_at == "2026-08-31"
    assert c.evidence_data.get("identity_evidence") is True
    # Empty companies list (real spike shape) -> no candidates.
    assert yc_to_candidates([], acct, today=today) == []


def test_adapter_plan_url():
    src = YcBatchSource()
    assert src.key == "yc_batch"
    assert src.tier == "http"
    assert src.cadence_hours == 720
    acct = Account(domain="acmerockets.example", name="Acme Rockets")
    tasks = src.plan(acct, None)
    assert len(tasks) == 1
    assert tasks[0].url == YC_BATCH_URL.format(batch="S24")
    assert tasks[0].url == "https://www.ycombinator.com/companies?batch=S24"
    # Batch override via extra_data.
    acct2 = Account(domain="acme.com", name="Acme", extra_data={"yc_batch": "W26"})
    assert src.plan(acct2, None)[0].url == "https://www.ycombinator.com/companies?batch=W26"


def test_adapter_parse_end_to_end():
    src = YcBatchSource()
    acct = Account(domain="acmerockets.example", name="Acme Rockets")
    doc = _FakeDoc(FIXTURE.read_bytes())
    cands = src.parse(doc, acct, {"today": "2026-08-31"})
    assert [c.natural_key for c in cands] == ["yc:acme-rockets"]
    # Real spike shape through the adapter: no candidates, no error.
    assert src.parse(_FakeDoc(REAL_SHAPE.encode()), acct, {"today": "2026-08-31"}) == []
    # Empty body: no candidates.
    assert src.parse(_FakeDoc(b""), acct, {"today": "2026-08-31"}) == []


class _FakeDoc:
    def __init__(self, body: bytes):
        self.body = body

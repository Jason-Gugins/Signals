from datetime import date
from pathlib import Path
import yaml
from src.sources.owned.ingest import aggregate_intent, classify_page, read_drop, resolve_visitor

RULES = yaml.safe_load(Path("config/owned_pages.yaml").read_text(encoding="utf-8"))


def test_read_csv_jsonl_and_drop_gmail():
    csv_rows = read_drop("tests/fixtures/owned/visits.csv")
    json_rows = read_drop("tests/fixtures/owned/events.jsonl")
    assert csv_rows and json_rows
    assert resolve_visitor({"email": "bob@gmail.com"})[0] is None
    assert resolve_visitor({"email": "jane@acme.com"})[0] == "acme.com"
    assert resolve_visitor({"ip": "1.2.3.4"}, ptr_lookup=lambda ip: "host.shopify.com")[0] == "shopify.com"


def test_page_classes():
    shapes = {
        "https://x.com/pricing": "pricing",
        "https://x.com/plans": "pricing",
        "https://x.com/demo": "demo",
        "https://x.com/product/foo": "product",
        "https://x.com/docs/api": "docs",
        "https://x.com/careers": "careers",
        "https://x.com/blog/hi": "blog",
        "https://x.com/": "home",
        "https://x.com/about": "other",
        "https://x.com/jobs": "careers",
    }
    for url, cls in shapes.items():
        assert classify_page(url, RULES) == cls, url


def test_aggregate_and_idempotent():
    rows = read_drop("tests/fixtures/owned/visits.csv")
    a = aggregate_intent(rows, today=date(2026, 8, 16), page_rules=RULES)
    b = aggregate_intent(rows, today=date(2026, 8, 16), page_rules=RULES)
    assert a and a[0][0] == "acme.com"
    assert a[0][1].evidence_data["visits"] == 2
    assert [x[1].natural_key for x in a] == [x[1].natural_key for x in b]
    assert a[0][1].signal_type == "intent_1st_owned"

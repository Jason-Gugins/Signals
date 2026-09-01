"""Tests for wayback pricing-page change detection (Task 17)."""

from src.sources.base import Account, Document
from src.sources.wayback.collector import WaybackSource
from src.sources.wayback.pricing import diff_pricing

PREV_PRICING = """<html><body>
<div id="pricing">
  <div class="plan"><h3>Starter</h3><p>$29/mo</p></div>
  <div class="plan"><h3>Growth</h3><p>$99/mo</p></div>
</div>
</body></html>"""

CURR_PRICING = """<html><body>
<div id="pricing">
  <div class="plan"><h3>Starter</h3><p>$29/mo</p></div>
  <div class="plan"><h3>Scale</h3><p>$149/mo</p></div>
</div>
</body></html>"""

NO_CONTAINER = "<html><body><p>Welcome to our site. No pricing here.</p></body></html>"


def test_changed_plan_names_emit_candidate():
    cand = diff_pricing(PREV_PRICING, CURR_PRICING, domain="acme.com", today="2026-08-31")
    assert cand is not None
    assert cand.signal_type == "pricing_change"
    assert cand.natural_key == "pricediff:acme.com:2026-08-31"
    assert cand.confidence == 0.65
    assert cand.observed_at == "2026-08-31"


def test_identical_html_returns_none():
    assert diff_pricing(PREV_PRICING, PREV_PRICING, domain="acme.com", today="2026-08-31") is None


def test_missing_container_in_prev_returns_none():
    assert diff_pricing(NO_CONTAINER, CURR_PRICING, domain="acme.com", today="2026-08-31") is None


def test_missing_container_in_curr_returns_none():
    assert diff_pricing(PREV_PRICING, NO_CONTAINER, domain="acme.com", today="2026-08-31") is None


def test_empty_or_none_inputs_return_none():
    assert diff_pricing("", CURR_PRICING, domain="acme.com", today="2026-08-31") is None
    assert diff_pricing(None, CURR_PRICING, domain="acme.com", today="2026-08-31") is None
    assert diff_pricing(PREV_PRICING, "", domain="acme.com", today="2026-08-31") is None


def test_garbage_html_returns_none():
    assert diff_pricing("<div", CURR_PRICING, domain="acme.com", today="2026-08-31") is None
    assert diff_pricing(PREV_PRICING, b"\xff\xfe\x00garbage", domain="acme.com", today="2026-08-31") is None


# --- collector wiring ------------------------------------------------------


def _cdx_with_pricing() -> bytes:
    return (
        b'[["timestamp","original","digest","statuscode"],'
        b'["20200101120000","http://acme.com/","aaa","200"],'
        b'["20240101120000","http://acme.com/pricing","pp1","200"],'
        b'["20230101120000","http://acme.com/pricing","pp0","200"]]'
    )


def test_follow_emits_pricing_task_for_pricing_cdx_rows():
    wb = WaybackSource()
    doc = Document(doc_id="c", source="wayback", body=_cdx_with_pricing())
    follows = wb.follow_tasks(doc, Account(domain="acme.com"), {"kind": "cdx"})
    pricing = [t for t in follows if (t.meta or {}).get("kind") == "pricing"]
    assert len(pricing) == 1
    assert "pricing" in (pricing[0].url or "")
    # latest snapshot wins
    assert "20240101120000" in pricing[0].url


def test_follow_without_pricing_rows_emits_no_pricing_task():
    wb = WaybackSource()
    doc = Document(doc_id="c", source="wayback", body=b'[["timestamp","original"],["20200101120000","http://acme.com/"]]')
    follows = wb.follow_tasks(doc, Account(domain="acme.com"), {"kind": "cdx"})
    assert not [t for t in follows if (t.meta or {}).get("kind") == "pricing"]


def test_parse_pricing_without_previous_html_returns_empty():
    wb = WaybackSource()
    doc = Document(doc_id="p", source="wayback", body=CURR_PRICING.encode("utf-8"))
    cands = wb.parse(doc, Account(domain="acme.com"), {"kind": "pricing", "today": "2026-08-31"})
    assert cands == []


def test_parse_pricing_with_previous_html_diffs():
    wb = WaybackSource()
    doc = Document(doc_id="p", source="wayback", body=CURR_PRICING.encode("utf-8"))
    cands = wb.parse(
        doc,
        Account(domain="acme.com"),
        {"kind": "pricing", "today": "2026-08-31", "prev_pricing_html": PREV_PRICING},
    )
    assert len(cands) == 1
    assert cands[0].signal_type == "pricing_change"
    assert cands[0].natural_key == "pricediff:acme.com:2026-08-31"


def test_parse_non_pricing_doc_still_returns_empty():
    wb = WaybackSource()
    doc = Document(doc_id="p", source="wayback", body=CURR_PRICING.encode("utf-8"))
    assert wb.parse(doc, Account(domain="acme.com"), {"kind": "snapshot", "today": "2026-08-31"}) == []

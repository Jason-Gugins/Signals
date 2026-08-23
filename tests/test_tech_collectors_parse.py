from pathlib import Path

from src.core.models import Account, Document
from src.sources.crtsh.collector import CrtshSource
from src.sources.techstack.collector import TechstackSource
from src.sources.wayback.collector import WaybackSource


def test_techstack_parse_homepage():
    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    src = TechstackSource()
    cands = src.parse(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=html),
        Account(domain="acme.com"),
        {"today": "2026-08-16"},
    )
    assert any(c.signal_type == "tech_install_new" for c in cands)


def test_techstack_parse_network_fixture():
    body = Path("tests/fixtures/techstack/network_scanner.dev.json").read_bytes()
    src = TechstackSource()
    cands = src.parse(
        Document(doc_id="n", source="techstack", url="https://scanner.dev/", body=body, content_type="application/json"),
        Account(domain="scanner.dev"),
        {"today": "2026-08-16", "kind": "network"},
    )
    titles = {c.title for c in cands}
    assert "webflow" in titles
    assert any(c.signal_type == "tech_install_new" for c in cands)


def test_parse_network_does_not_signal_raw_hosts():
    body = Path("tests/fixtures/techstack/network_scanner.dev.json").read_bytes()
    cands = TechstackSource().parse(
        Document(doc_id="n", source="techstack", url="https://scanner.dev/", body=body),
        Account(domain="scanner.dev"),
        {"today": "2026-08-16", "kind": "network"},
    )
    titles = {c.title for c in cands}
    assert "webflow" in titles and "hubspot" in titles
    assert not any(str(t).startswith("host:") for t in titles)


def test_techstack_plan_emits_html_and_network():
    src = TechstackSource()
    tasks = src.plan(Account(domain="acme.com"), None)
    kinds = [(t.meta or {}).get("kind") for t in tasks]
    assert kinds == ["html", "network"]
    assert all(t.url == "https://acme.com/" for t in tasks)
    assert (tasks[1].meta or {}).get("capture") == "network"


def test_wayback_follow_and_crtsh():
    cdx = Path("tests/fixtures/wayback/cdx.json").read_bytes()
    wb = WaybackSource()
    doc = Document(doc_id="c", source="wayback", body=cdx)
    assert wb.parse(doc, Account(domain="acme.com"), {"today": "2026-08-16", "kind": "cdx"}) == []
    follows = wb.follow_tasks(doc, Account(domain="acme.com"), {"kind": "cdx"})
    assert follows and len(follows) <= 12
    names = Path("tests/fixtures/crtsh/crtsh.json").read_bytes()
    cands = CrtshSource().parse(
        Document(doc_id="r", source="crtsh", body=names),
        Account(domain="acme.com"),
        {"today": "2026-08-16"},
    )
    assert cands
    assert all(c.confidence <= 0.5 for c in cands)

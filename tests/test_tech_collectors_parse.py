from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.sources.crtsh.collector import CrtshSource
from src.sources.techstack.collector import TechstackSource, upsert_technologies
from src.sources.wayback.collector import WaybackSource


def test_techstack_parse_homepage():
    html = Path("tests/fixtures/techstack/homepage.html").read_bytes()
    src = TechstackSource()
    cands = src.parse(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=html),
        Account(domain="acme.com"),
        {"today": "2026-08-16"},
    )
    # tech_install_new is owned by the prior-cycle diff pass (first-seen
    # semantics) — parse must not re-fire installs on every collect. This
    # fixture has only mid-tier matches (hubspot), so parse emits nothing.
    assert not any(c.signal_type == "tech_install_new" for c in cands)
    assert cands == []


def test_techstack_parse_network_fixture():
    body = Path("tests/fixtures/techstack/network_scanner.dev.json").read_bytes()
    src = TechstackSource()
    cands = src.parse(
        Document(doc_id="n", source="techstack", url="https://scanner.dev/", body=body, content_type="application/json"),
        Account(domain="scanner.dev"),
        {"today": "2026-08-16", "kind": "network"},
    )
    # tech_install_new is owned by the prior-cycle diff pass (first-seen
    # semantics) — parse must not re-fire installs on every collect. With no
    # enterprise-tier or competitor match in this fixture (webflow/hubspot
    # naming lives on the harvest side, see test_harvest_tech_upserts_observed),
    # the network parse emits no candidates at all.
    assert not any(c.signal_type == "tech_install_new" for c in cands)
    assert cands == []


def test_parse_network_does_not_signal_raw_hosts():
    body = Path("tests/fixtures/techstack/network_scanner.dev.json").read_bytes()
    cands = TechstackSource().parse(
        Document(doc_id="n", source="techstack", url="https://scanner.dev/", body=body),
        Account(domain="scanner.dev"),
        {"today": "2026-08-16", "kind": "network"},
    )
    titles = {c.title for c in cands}
    # Raw hosts stay inventory (host:...) and never become parse signals —
    # and with installs owned by the diff pass, this fixture yields no parse
    # candidates at all (vendor naming is pinned on the harvest side in
    # test_harvest_tech_upserts_observed).
    assert not any(str(t).startswith("host:") for t in titles)
    assert cands == []


def test_techstack_plan_emits_html_and_network():
    src = TechstackSource()
    tasks = src.plan(Account(domain="acme.com"), None)
    kinds = [(t.meta or {}).get("kind") for t in tasks]
    assert kinds == ["html", "network"]
    assert all(t.url == "https://acme.com/" for t in tasks)
    assert (tasks[1].meta or {}).get("capture") == "network"


def test_harvest_tech_upserts_observed(tmp_path):
    db = Database(tmp_path / "s.db")
    src = TechstackSource()
    body = Path("tests/fixtures/techstack/network_scanner.dev.json").read_bytes()
    doc = Document(doc_id="n", source="techstack", url="https://scanner.dev/", body=body)
    ms = src.harvest_tech(doc, Account(domain="scanner.dev"), {"kind": "network", "today": "2026-08-16"})
    upsert_technologies(db, "scanner.dev", ms, now="2026-08-16T00:00:00")
    rows = db.query("SELECT vendor, tier FROM technologies WHERE domain=?", ("scanner.dev",))
    vendors = {r["vendor"] for r in rows}
    assert "hubspot" in vendors
    assert any(v.startswith("host:") for v in vendors)


def test_harvest_tech_after_bypass_keeps_all_vendors(tmp_path):
    # body is real homepage HTML (post-bypass) with HubSpot + GTM scripts
    html = b"<html><script src='https://js.hs-scripts.com/x.js'></script><script src='https://www.googletagmanager.com/gtm.js'></script></html>"
    src = TechstackSource()
    matches = src.harvest_tech(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=html),
        Account(domain="acme.com"),
        {"today": "2026-08-23"},
    )
    vendors = {m.vendor for m in matches}
    assert "hubspot" in vendors
    assert "google_tag_manager" in vendors or "gtm" in vendors or any("googletagmanager" in str(m.evidence) for m in matches)


def test_harvest_tech_challenge_unsolved_records_cloudflare_only(tmp_path):
    body = b"<html><title>Just a moment...</title></html>"
    src = TechstackSource()
    matches = src.harvest_tech(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=body),
        Account(domain="acme.com"),
        {"today": "2026-08-23", "cloudflare_unsolved": True},
    )
    vendors = {m.vendor for m in matches}
    assert vendors == {"cloudflare"}  # honest hard stop


def test_parse_challenge_unsolved_emits_no_tech_install_new():
    body = b"<html><title>Just a moment...</title></html>"
    cands = TechstackSource().parse(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=body),
        Account(domain="acme.com"),
        {"today": "2026-08-23", "cloudflare_unsolved": True},
    )
    assert not any(c.signal_type == "tech_install_new" for c in cands)


def test_parse_does_not_emit_tech_install_new(monkeypatch):
    # The prior-cycle diff pass (diff_technologies) is the sole owner of
    # tech_install_new — only it knows first-seen. Parse must never emit
    # installs, while high_ticket_tech (enterprise tier) and
    # competitor_detected (vendor in rules['competitors']) keep firing for
    # vendors whose spec matches.
    import src.sources.techstack.fingerprint as fingerprint

    rules = {
        "version": 1,
        "vendors": {
            "salesforce": {
                "display": "Salesforce",
                "category": ["crm"],
                "tier": "enterprise",
                "match": {"script_src": ["script.example.com/sf.js"]},
            }
        },
        "competitors": ["salesforce"],
    }
    monkeypatch.setattr(fingerprint, "load_fingerprint_rules", lambda: rules)
    body = b"<html><head><script src='https://script.example.com/sf.js'></script></head><body><p>hello</p></body></html>"
    cands = TechstackSource().parse(
        Document(doc_id="h", source="techstack", url="https://acme.com/", body=body),
        Account(domain="acme.com", name="Acme"),
        {"kind": "html", "today": "2026-10-03"},
    )
    assert not any(c.signal_type == "tech_install_new" for c in cands)
    tickets = [c for c in cands if c.signal_type == "high_ticket_tech"]
    assert [c.natural_key for c in tickets] == ["high_ticket_tech:salesforce:2026"]
    comps = [c for c in cands if c.signal_type == "competitor_detected"]
    assert [c.natural_key for c in comps] == ["competitor_detected:salesforce:2026-10"]


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

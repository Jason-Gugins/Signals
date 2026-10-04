from datetime import date
from src.core.db import Database
from src.sources.techstack.collector import tech_to_candidates, upsert_technologies
from src.sources.techstack.fingerprint import (
    HttpEvidence,
    TechMatch,
    dynamic_matches,
    extract_http_evidence,
    extract_network_evidence,
    match_fingerprints,
    observed_hosts,
)
import yaml
from pathlib import Path

RULES = yaml.safe_load(Path("config/fingerprints.yaml").read_text(encoding="utf-8"))
NET_FIX = Path("tests/fixtures/techstack/network_scanner.dev.json")


def test_extract_network_evidence_from_fixture():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    assert "cdn.prod.website-files.com" in ev.hosts
    assert ev.page_url
    assert all("?" not in u for u in ev.urls)


def test_observed_hosts_drops_first_party():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    hosts = observed_hosts(ev, domain="scanner.dev")
    assert "scanner.dev" not in hosts
    assert "cdn.prod.website-files.com" in hosts
    assert "js.hsforms.net" in hosts


def test_dynamic_matches_are_unknown_tier():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    ms = dynamic_matches(ev, domain="scanner.dev")
    assert any(m.vendor == "host:cdn-cookieyes.com" for m in ms)
    assert all(m.tier == "unknown" for m in ms)
    assert not any(m.vendor.startswith("host:scanner.dev") for m in ms)


def test_dynamic_matches_label_script_src_hosts():
    # HttpEvidence: hostname extracted from <script src>, no hosts attr
    ev = extract_http_evidence(
        b"<html><script src='https://ajax.googleapis.com/ajax/libs/x.js'></script></html>",
        {}, "https://acme.com/",
    )
    ms = dynamic_matches(ev, domain="acme.com")
    row = [m for m in ms if m.vendor == "host:ajax.googleapis.com"][0]
    assert row.evidence == "script_src"
    assert row.confidence == 0.4
    assert row.tier == "unknown"


def test_dynamic_matches_label_network_hosts():
    # NetworkEvidence: HAR-lite fixture hosts keep the network_host label
    ev = extract_network_evidence(NET_FIX.read_bytes())
    ms = dynamic_matches(ev, domain="scanner.dev")
    row = [m for m in ms if m.vendor == "host:cdn-cookieyes.com"][0]
    assert row.evidence == "network_host"


def test_observed_host_sources_mixed_object_labels_each_channel():
    # Defensive: a synthetic evidence carrying BOTH channels labels each
    # host from its own channel (network first, matching current ordering).
    ev = extract_http_evidence(
        b"<html><script src='https://ajax.googleapis.com/x.js'></script></html>",
        {}, "https://acme.com/",
    )
    ev.hosts = ("cdn-cookieyes.com",)  # synthetic dual-channel object
    from src.sources.techstack.fingerprint import _observed_host_items

    items = dict(_observed_host_items(ev, domain="acme.com"))
    assert items["ajax.googleapis.com"] == "script_src"
    assert items["cdn-cookieyes.com"] == "network_host"


def test_named_majority_from_scanner_fixture():
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    rules = load_fingerprint_rules()
    ev = extract_network_evidence(NET_FIX.read_bytes())
    named = {m.vendor for m in match_fingerprints(ev, rules)}
    assert {"webflow", "hubspot", "gtm", "google_analytics", "meta_pixel", "cookieyes", "vector"} <= named


def test_probe_fixture_names_onetrust_bing_vimeo():
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    rules = load_fingerprint_rules()
    lev = extract_network_evidence(Path("tests/fixtures/techstack/network_levitate.ai.json").read_bytes())
    dark = extract_network_evidence(Path("tests/fixtures/techstack/network_darktrace.com.json").read_bytes())
    lev_named = {m.vendor for m in match_fingerprints(lev, rules)}
    dark_named = {m.vendor for m in match_fingerprints(dark, rules)}
    assert "onetrust" in lev_named
    assert {"onetrust", "bing_uet", "vimeo"} <= dark_named


def test_challenge_host_names_cloudflare():
    from src.sources.techstack.fingerprint import NetworkEvidence, is_challenge_evidence, load_fingerprint_rules

    ev = NetworkEvidence(page_url="https://example.com/", hosts=("challenges.cloudflare.com",), urls=())
    assert is_challenge_evidence(ev) is True
    assert is_challenge_evidence(ev, status=403) is True
    named = {m.vendor for m in match_fingerprints(ev, load_fingerprint_rules())}
    assert "cloudflare" in named


def test_promote_drops_duplicate_host_row():
    from src.sources.techstack.fingerprint import load_fingerprint_rules, promote_or_observe

    ev = extract_network_evidence(NET_FIX.read_bytes())
    ms = promote_or_observe(ev, load_fingerprint_rules(), domain="scanner.dev")
    vendors = [m.vendor for m in ms]
    assert "hubspot" in vendors
    assert "host:js.hsforms.net" not in vendors
    assert "host:cdn-cookieyes.com" not in vendors
    assert any(v.startswith("host:") for v in vendors)


def test_match_network_hosts_webflow_from_fixture():
    ev = extract_network_evidence(NET_FIX.read_bytes())
    hits = match_fingerprints(ev, RULES)
    assert any(m.vendor == "webflow" and m.evidence == "network_host" for m in hits)


def test_network_host_suffix_does_not_hit_workforce():
    from src.sources.techstack.fingerprint import NetworkEvidence

    ev = NetworkEvidence(page_url="https://x.com/", hosts=("workforce.com",), urls=())
    rules = {"vendors": {"salesforce": {"display": "Salesforce", "match": {"network_host": ["force.com"]}}}}
    assert match_fingerprints(ev, rules) == []


def test_upsert_and_disappear(tmp_path):
    db = Database(tmp_path / "s.db")
    m = TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script", 0.8)
    new, gone = upsert_technologies(db, "acme.com", [m], now="2026-08-01")
    assert new == ["hubspot"] and gone == []
    new, gone = upsert_technologies(db, "acme.com", [], now="2026-08-08")
    assert gone == []
    new, gone = upsert_technologies(db, "acme.com", [], now="2026-08-15")
    assert "hubspot" in gone


def test_host_rows_pruned_after_six_missing_runs(tmp_path):
    # Task 9 pin: host: inventory rows absent for 6 consecutive runs are
    # pruned (deleted) by upsert_technologies — they are observed-host noise,
    # not removal signals, so they never enter `gone`. Named rows are NEVER
    # deleted: they keep feeding the gone-list (tech_removed path) forever.
    db = Database(tmp_path / "s.db")
    h = TechMatch("host:cdn.old-vendor.io", "cdn.old-vendor.io", ["observed"], "unknown", "network_host", 0.4)
    n = TechMatch("hubspot", "HubSpot", ["crm"], "mid", "script_src", 0.8)
    upsert_technologies(db, "acme.com", [h, n], now="2026-08-01")
    # 5 misses: both rows still present, missing_runs == 5
    for i in range(5):
        upsert_technologies(db, "acme.com", [], now=f"2026-09-0{i + 1}")
    rows = {
        r["vendor"]: r["missing_runs"]
        for r in db.query("SELECT vendor, missing_runs FROM technologies WHERE domain=?", ("acme.com",))
    }
    assert rows == {"host:cdn.old-vendor.io": 5, "hubspot": 5}
    # 6th miss: host: row DELETED (pruned silently, not in gone), named row kept
    new, gone = upsert_technologies(db, "acme.com", [], now="2026-09-06")
    rows = {r["vendor"] for r in db.query("SELECT vendor FROM technologies WHERE domain=?", ("acme.com",))}
    assert rows == {"hubspot"}
    assert gone == ["hubspot"]  # named removal still signals (tech_removed path)
    # Negative pin: a named row driven past 6 misses is never deleted
    for i in range(7, 10):
        upsert_technologies(db, "acme.com", [], now=f"2026-09-{i:02d}")
    rows = {r["vendor"] for r in db.query("SELECT vendor FROM technologies WHERE domain=?", ("acme.com",))}
    assert rows == {"hubspot"}


def test_high_ticket_once_per_year():
    rows = [{"vendor": "salesforce", "tier": "enterprise", "first_seen_at": "2026-01-01"}]
    c1 = tech_to_candidates("acme.com", ["salesforce"], [], rows, RULES, [], today=date(2026, 8, 16))
    types = [c.signal_type for c in c1]
    # tech_install_new is diff-owned now (first-seen); parse-time candidates
    # carry only the tier/competitor emissions.
    assert "tech_install_new" not in types and "high_ticket_tech" in types
    # same natural_key year means store would dedupe
    assert [c.natural_key for c in c1 if c.signal_type == "high_ticket_tech"][0].endswith("2026")


from src.sources.techstack.fingerprint import classify_cloudflare_challenge


def test_classify_403_is_managed_challenge():
    assert classify_cloudflare_challenge(status=403, body=b"") == "managed"


def test_classify_just_a_moment_is_js_challenge():
    body = b"<html><title>Just a moment...</title></html>"
    assert classify_cloudflare_challenge(status=200, body=body) == "js"


def test_classify_challenges_host_is_js_challenge():
    # NetworkEvidence-style: hosts tuple includes the challenge host
    assert classify_cloudflare_challenge(status=200, body=b"", challenge_host="challenges.cloudflare.com") == "js"


def test_classify_turnstile_text_is_managed():
    body = b"<html>Press & Hold to confirm you are human</html>"
    assert classify_cloudflare_challenge(status=200, body=body) == "managed"


def test_classify_no_challenge_returns_none():
    assert classify_cloudflare_challenge(status=200, body=b"<html>real site</html>") is None


def test_classify_disabled_when_status_200_and_no_markers():
    assert classify_cloudflare_challenge(status=200, body=b"") is None


def test_html_marker_evidence_matches():
    ev = extract_http_evidence(b"<html><body data-shopify><script src='/x.js'></script></body></html>", {}, "https://d.com/")
    spec = {"shopify": {"match": {"html_marker": ["data-shopify"]}, "tier": "mid", "category": ["ecommerce"]}}
    hits = match_fingerprints(ev, {"vendors": spec})
    assert [h.vendor for h in hits] == ["shopify"]
    assert hits[0].evidence == "html_marker"
    assert hits[0].confidence == 0.8


def test_html_marker_no_false_match_on_other_vendors():
    ev = extract_http_evidence(b"<html><body><p>plain marketing page</p></body></html>", {}, "https://d.com/")
    spec = {
        "shopify": {"match": {"html_marker": ["data-shopify"]}, "tier": "mid", "category": ["ecommerce"]},
        "wordpress": {"match": {"html_marker": ["/wp-content/"]}, "tier": "low", "category": ["cms"]},
    }
    assert match_fingerprints(ev, {"vendors": spec}) == []


def test_inline_global_evidence():
    # Substring contract (Revision 1): inline_global matching is substring on
    # a joined blob, not exact list membership — the stored global may keep
    # the call form with its trailing paren while the YAML needle is the bare
    # function name.
    ev = HttpEvidence(
        url="https://d.com/",
        headers={},
        cookies=[],
        script_srcs=[],
        link_hrefs=[],
        meta={},
        inline_globals=["hbspt.forms.create("],
        text_sample="",
    )
    spec = {"hubspot": {"match": {"inline_global": ["hbspt.forms.create"]}, "tier": "mid", "category": ["crm"]}}
    hits = match_fingerprints(ev, {"vendors": spec})
    assert [h.vendor for h in hits] == ["hubspot"]
    assert hits[0].evidence == "inline_global"
    assert hits[0].confidence == 0.7
    # End to end: extract_http_evidence hoists the _INITS call into globals.
    ev2 = extract_http_evidence(
        b"<html><body><script>hbspt.forms.create({portalId:1});</script></body></html>", {}, "https://d.com/"
    )
    assert "hbspt.forms.create" in ev2.inline_globals
    hits2 = match_fingerprints(ev2, {"vendors": spec})
    assert [h.vendor for h in hits2] == ["hubspot"]
    assert hits2[0].evidence == "inline_global"
    assert hits2[0].confidence == 0.7


def test_cookie_name_evidence():
    ev = HttpEvidence(
        url="https://d.com/",
        headers={},
        cookies=["_shopify_s", "_shopify_y"],
        script_srcs=[],
        link_hrefs=[],
        meta={},
        inline_globals=[],
        text_sample="",
    )
    spec = {"shopify": {"match": {"cookie_name": ["_shopify_s"]}, "tier": "mid", "category": ["ecommerce"]}}
    hits = match_fingerprints(ev, {"vendors": spec})
    assert [h.vendor for h in hits] == ["shopify"]
    assert hits[0].evidence == "cookie_name"
    assert hits[0].confidence == 0.7


def test_meta_generator_evidence():
    ev = HttpEvidence(
        url="https://d.com/",
        headers={},
        cookies=[],
        script_srcs=[],
        link_hrefs=[],
        meta={"generator": "WordPress 6.5"},
        inline_globals=[],
        text_sample="",
    )
    spec = {"wordpress": {"match": {"meta_generator": ["wordpress"]}, "tier": "low", "category": ["cms"]}}
    hits = match_fingerprints(ev, {"vendors": spec})
    assert [h.vendor for h in hits] == ["wordpress"]
    assert hits[0].evidence == "meta_generator"
    assert hits[0].confidence == 0.7


def test_enterprise_vendors_carry_explicit_contract_years():
    # Task 7 pin: renewal_window estimates default to 1-year contracts, which
    # understates multi-year enterprise terms. Presence of the key (not its
    # value — those are judgment calls) is what this pins, so any future
    # tier: enterprise vendor must state its renewal horizon explicitly.
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    rules = load_fingerprint_rules()
    vendors = rules.get("vendors") or {}
    ent = {k: s for k, s in vendors.items() if (s or {}).get("tier") == "enterprise"}
    assert ent, "no enterprise vendors found"
    for name, spec in ent.items():
        cy = (spec or {}).get("contract_years")
        assert isinstance(cy, int) and cy >= 1, f"{name} (tier=enterprise) needs an explicit contract_years >= 1"


def test_load_rules_cached_same_mtime():
    # Task 8 pin: consecutive calls with an unchanged fingerprints.yaml must
    # reuse the parsed dict (same object), not re-read/re-parse per call.
    from src.sources.techstack import fingerprint
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    try:
        r1 = load_fingerprint_rules()
        r2 = load_fingerprint_rules()
        assert r1 is r2
    finally:
        fingerprint._RULES_CACHE = None


def test_load_rules_reloads_on_mtime_change():
    # Cache key is the file mtime: a stale sentinel cache entry (mtime 0.0,
    # which can never match the real file) must be replaced by a fresh parse.
    from src.sources.techstack import fingerprint
    from src.sources.techstack.fingerprint import load_fingerprint_rules

    try:
        load_fingerprint_rules()
        fingerprint._RULES_CACHE = (0.0, {"stale": True})
        rules = load_fingerprint_rules()
        assert rules is not None and rules != {"stale": True}
        assert "webflow" in (rules.get("vendors") or {})
    finally:
        fingerprint._RULES_CACHE = None

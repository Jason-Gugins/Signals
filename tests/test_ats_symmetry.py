"""ATS detection/collector symmetry.

Detection patterns must exist for every vendor that has a collector
(rippling/jobvite/breezy were never detected), and detections for
collector-less vendors (bamboohr/jazzhr/personio) must NOT stamp
ats_vendor/ats_token — either field disables the ats_careers_page
fallback (src/sources/registry.py _ats_vendor_ok requires BOTH empty),
which would silently stop hiring collection for the account.
"""

from __future__ import annotations

from pathlib import Path

from src.identity.ats_discovery import AtsDiscovery, detect_ats


FIXTURES = Path(__file__).parent / "fixtures" / "ats"


# ── Part A: detection patterns for collector-backed vendors ────────────────

def test_rippling_pattern_detected():
    html = '<html><body><a href="https://ats.rippling.com/acme">Careers</a></body></html>'
    matches = detect_ats(html, "https://acme.com/careers")
    assert matches, "rippling not detected"
    assert matches[0].vendor == "rippling"
    assert matches[0].token == "acme"


def test_jobvite_pattern_detected():
    html = '<html><body><a href="https://jobs.jobvite.com/acme/jobs">Careers</a></body></html>'
    matches = detect_ats(html, "https://acme.com/careers")
    assert matches, "jobvite not detected"
    assert matches[0].vendor == "jobvite"
    assert matches[0].token == "acme"


def test_breezy_pattern_detected():
    html = '<html><body><a href="https://acme.breezy.hr/">Careers</a></body></html>'
    matches = detect_ats(html, "https://acme.com/careers")
    assert matches, "breezy not detected"
    assert matches[0].vendor == "breezy"
    assert matches[0].token == "acme"


# ── Part B: collector-less detections stay vendorless on the write path ────

def _discover(tmp_path, html):
    """Drive the full discover() write path (mirrors test_ats_discovery.py)."""
    from src.core.db import Database
    from src.core.http import FetchResult
    from src.core.models import Account, Document
    from src.identity.registry import AccountRegistry

    class Fake:
        def get(self, task, **kw):
            doc = Document(doc_id="c", source="ats_discovery", url=task.url, body=html.encode(), status=200)
            return FetchResult(True, 200, doc, False, None, 1)

    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    acct = Account(domain="acme.com", careers_url="https://acme.com/careers")
    reg.upsert(acct)
    match = AtsDiscovery(Fake(), reg).discover(acct)
    return match, reg.get("acme.com")


def test_bamboohr_hit_keeps_careers_fallback_eligible(tmp_path):
    html = (FIXTURES / "careers_bamboohr.html").read_text(encoding="utf-8")
    match, stored = _discover(tmp_path, html)
    assert match is not None and match.vendor == "bamboohr"  # detection still works
    assert not (stored.ats_vendor or "").strip(), "bamboohr has no collector; stamping it disables the careers fallback"
    assert not (stored.ats_token or "").strip()
    assert stored.careers_url == "https://acme.com/careers"


def test_greenhouse_hit_still_stamps_vendor_and_token(tmp_path):
    html = (FIXTURES / "careers_greenhouse.html").read_text(encoding="utf-8")
    match, stored = _discover(tmp_path, html)
    assert match is not None and match.vendor == "greenhouse"
    assert stored.ats_vendor == "greenhouse"
    assert stored.ats_token == "acme"

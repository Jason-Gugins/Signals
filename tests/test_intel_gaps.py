"""Finding 9: the dossier must say WHY `needs` promoted nothing.

A missing/empty market profile makes ``NeedsSource.local_harvest`` return []
early (no relevance vocabulary means nothing can EVER be promoted), but the
dossier only showed ``market profile: -``: a reader could not tell "no
first-party evidence" from "no profile configured". These tests pin the gap
line that names the reason AND the honest number of first-party documents the
harvester could actually see (the same resolver ``needs`` uses).

Offline: a temp SQLite database plus the recording-double orchestrator style of
``tests/test_intel.py``. The only production code under test is ``run_intel``;
the package seams are monkeypatched so nothing is written to disk.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account
from src.core.rawstore import RawStore
from src.pipeline import intel
from src.pipeline.runner import RunnerStats
from src.sources.needs.collector import _first_party_doc_ids

DOMAIN = "acme.com"


def _body(i: int) -> bytes:
    """A per-document body: RawStore keys documents by content hash, so two
    identical bodies are ONE document (and the count must not pretend
    otherwise)."""
    return (
        f"<article><p>Post {i}. We are consolidating data across teams.</p></article>"
    ).encode("utf-8")

#: The new gap line, and ONLY the new gap line: the pre-existing gap says
#: "no relevance vocabulary configured: market profile 'default' is empty".
GAP_RE = re.compile(r"no market profile configured")


class _Registry:
    def __init__(self, accounts):
        self.accounts = dict(accounts or {})

    def get(self, domain):
        return self.accounts.get(domain)

    def upsert(self, account, *, source=None):
        self.accounts[account.domain] = account
        return account


class FakeOrch:
    """Recording-double orchestrator over a REAL (temp) database."""

    def __init__(self, db, *, snapshot=None):
        self.registry = _Registry({DOMAIN: Account(domain=DOMAIN, name="Acme")})
        self.db = db
        self.config = Config()
        self.snapshot = snapshot or SimpleNamespace(domain=DOMAIN, today=date(2026, 9, 15))
        self.calls = []

    def resolve(self, **kw):
        self.calls.append(("resolve", kw))
        return {"accounts": 1}

    def collect(self, **kw):
        self.calls.append(("collect", kw))
        stats = RunnerStats()
        stats.tasks = 1
        stats.signals_new = 0
        return stats

    def score(self, **kw):
        self.calls.append(("score", kw))
        return {"scored": 0, "snapshots": {DOMAIN: self.snapshot}}


class _Profile:
    """Stand-in for ``src.intel.market.MarketProfile`` (only ``is_empty`` is used)."""

    def __init__(self, profile_id="default", *, empty=False):
        self.profile_id = profile_id
        self.seller = "Seller"
        self.offerings = ()
        self._empty = empty

    @property
    def is_empty(self):
        return self._empty


def _patch_profile(monkeypatch, profile):
    """Resolve the market profile to ``profile`` (an empty one or a configured one)."""

    def fake_load(profile_id, path=None):
        return profile

    monkeypatch.setattr(intel, "load_market_profile", fake_load)


def _patch_package(monkeypatch):
    """Patch the package seams; return the recorder holding the gaps handed over."""
    rec = {}

    def fake_build_dossier(snapshot, **kw):
        rec["dossier"] = {
            "domain": getattr(snapshot, "domain", None),
            "gaps": list(kw.get("gaps") or []),
        }
        return rec["dossier"]

    monkeypatch.setattr(intel, "build_coverage", lambda **kw: [])
    monkeypatch.setattr(intel, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(
        intel, "write_intel_package", lambda dossier, *, out_dir: {"package_dir": "pkg"}
    )
    return rec


def _seed_first_party(db, tmp_path, count: int) -> None:
    """Store ``count`` first-party company_feed articles WITH fetch_log rows.

    Mirrors ``tests/test_needs_first_party_e2e.py``: a stored body plus the
    ``company_feed`` provenance row the resolver reads.
    """
    store = RawStore(db, tmp_path / "raw")
    for i in range(count):
        url = f"https://acme.com/blog/post-{i}"
        body = _body(i)
        store.put(
            source="company_feed",
            url=url,
            domain=DOMAIN,
            body=body,
            content_type="text/html",
            status=200,
        )
        db.execute(
            "INSERT INTO fetch_log(run_id, source, domain, url, status, bytes, at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("t1", "company_feed", DOMAIN, url, 200, len(body), "2026-09-15T01:12:51+00:00"),
        )
    # The count the dossier will report comes from THIS resolver, so ground the
    # expectation on it directly.
    assert len(_first_party_doc_ids(db, DOMAIN)) == count


def _run(orch, monkeypatch, profile):
    _patch_profile(monkeypatch, profile)
    rec = _patch_package(monkeypatch)
    result = intel.run_intel(
        DOMAIN, config=orch.config, orch=orch, write=False
    )
    return result, rec


def _gap_lines(gaps):
    return [gap for gap in gaps if GAP_RE.search(gap)]


def test_empty_profile_with_first_party_docs_names_the_real_count(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    _seed_first_party(db, tmp_path, 2)
    orch = FakeOrch(db)

    result, rec = _run(orch, monkeypatch, _Profile(empty=True))

    lines = _gap_lines(result["gaps"])
    assert len(lines) == 1, result["gaps"]
    line = lines[0]
    assert "2 first-party documents were in scope" in line, line
    assert "--market-profile" in line, line
    # The dossier the reader sees carries it too, not just the run result.
    assert line in rec["dossier"]["gaps"]
    # Reporting only: the run is otherwise unremarkable.
    assert result["errors"] == {}
    assert result["stages"]["derive"]["status"] == "ran"


def test_configured_profile_emits_no_profile_gap(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    _seed_first_party(db, tmp_path, 2)
    orch = FakeOrch(db)

    result, _ = _run(orch, monkeypatch, _Profile())

    assert _gap_lines(result["gaps"]) == [], result["gaps"]


def test_empty_profile_with_zero_first_party_docs_says_zero(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db")
    orch = FakeOrch(db)

    result, _ = _run(orch, monkeypatch, _Profile(empty=True))

    lines = _gap_lines(result["gaps"])
    assert len(lines) == 1, result["gaps"]
    assert "0 first-party documents in scope" in lines[0], lines[0]


def test_empty_profile_without_a_database_never_claims_a_count(tmp_path, monkeypatch):
    orch = FakeOrch(None)

    result, _ = _run(orch, monkeypatch, _Profile(empty=True))

    lines = _gap_lines(result["gaps"])
    assert len(lines) == 1, result["gaps"]
    assert "no market profile configured" in lines[0]
    assert not re.search(r"\d+ first-party documents", lines[0]), lines[0]


def test_dry_run_emits_no_profile_gap(tmp_path, monkeypatch):
    """A dry run never harvests, so it must not claim a promotion outcome."""
    db = Database(tmp_path / "s.db")
    _seed_first_party(db, tmp_path, 2)
    orch = FakeOrch(db)

    rec = _patch_package(monkeypatch)
    _patch_profile(monkeypatch, _Profile(empty=True))
    result = intel.run_intel(
        DOMAIN, config=orch.config, orch=orch, dry_run=True, write=False
    )

    assert result["stages"]["derive"]["status"] == "skipped"
    assert _gap_lines(result["gaps"]) == [], result["gaps"]
    assert "dossier" not in rec
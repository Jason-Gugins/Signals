"""Task 14: the offline end-to-end golden test for the intel master flow.

This is the ACCEPTANCE test for the whole feature. It drives the REAL
coordinator (:func:`src.pipeline.intel.run_intel`), the REAL collection path
over the two LOCAL sources (``jobsignals`` + ``needs``) and the REAL package
writer, with only the network-facing surfaces stubbed:

  * ``Orchestrator.resolve``      -- recorded and never called through to the
    real (network-facing) implementation.
  * ``Orchestrator.find_careers`` -- raises ``AssertionError`` if the flow ever
    reaches for a careers index.
  * ``src.pipeline.intel.load_market_profile`` -- returns a synthetic profile,
    because the shipped ``config/markets.yaml`` default profile is
    deliberately EMPTY (nothing can be promoted from it).

Everything else is the production code path: a temp SQLite database, the
configured ``RawStore``, the injected ``[JobSignalsSource(), NeedsSource()]``
adapter list (so no network-capable adapter is ever selected), the real
coverage builder and the real dossier/package writer.

``tests/conftest.py`` blocks httpx / curl_cffi / urllib / socket autouse, so a
real fetch here would ERROR rather than silently pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.core.config import Config
from src.core.db import Database
from src.core.models import Account
from src.core.rawstore import RawStore
from src.identity.registry import AccountRegistry
from src.intel.market import MarketProfile, Offering
from src.pipeline import intel
from src.pipeline.orchestrator import Orchestrator
from src.sources.jobsignals.collector import JobSignalsSource
from src.sources.needs.collector import NeedsSource


DOMAIN = "acme.com"
ACCOUNT_NAME = "Acme"

#: The two-sentence first-party document. The first sentence is a
#: profile-matching need; the second is unrelated and must NOT become one.
NEED_SENTENCE = "We are consolidating data across teams to improve reporting."
KITCHEN_SENTENCE = "We are redesigning the office kitchen this quarter."
FEED_BODY = f"{NEED_SENTENCE} {KITCHEN_SENTENCE}"
FEED_URL = "https://acme.com/blog/consolidating-data"

#: One open job whose description carries an explicit required-stack demand.
JOB_KEY = "acme-jobs-42"
JOB_TITLE = "Data Platform Engineer"
JOB_DEPARTMENT = "data"
JOB_URL = "https://acme.com/jobs/42"
VENDOR = "snowflake"
JOB_DESCRIPTION = "Required: hands-on experience with Snowflake."

#: The synthetic seller-market profile (the shipped default is empty).
OFFERING_ID = "signals-platform"
SERVED_PHRASE = "consolidating data across teams"

PACKAGE_FILES = (
    "manifest.json",
    "dossier.json",
    "dossier.md",
    "evidence.jsonl",
    "prompt.md",
)

#: Forbidden substrings: the artifact must never claim an absence.
_ABSENCE_SUBSTRINGS = ("absent", "not observed", "shift")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _synthetic_profile() -> MarketProfile:
    """A profile whose offering covers the need, the department and the vendor."""
    offering = Offering(
        offering_id=OFFERING_ID,
        buyer_departments=(JOB_DEPARTMENT,),
        served_problem_phrases=(SERVED_PHRASE,),
        required_vendors=(VENDOR,),
        competitor_vendors=(),
        relevant_signal_types=("need_statement", "required_stack_demand"),
    )
    return MarketProfile(profile_id="e2e", seller="Signals Co", offerings=(offering,))


def _walk_keys(obj):
    """Every dict key anywhere inside a JSON-shaped value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _collect_evidence_ids(obj, out: list) -> None:
    """Every ``evidence_ids`` value anywhere inside a JSON-shaped value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "evidence_ids":
                out.extend(value if isinstance(value, list) else [value])
            else:
                _collect_evidence_ids(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_evidence_ids(item, out)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _configured_source_keys(config_dir: str) -> set:
    table = yaml.safe_load(
        (Path(config_dir) / "sources.yaml").read_text(encoding="utf-8")
    )
    entries = table.get("sources", table)
    return {key for key, entry in (entries or {}).items() if isinstance(entry, dict)}


def _assert_self_contained(package_dir: Path) -> None:
    """Re-read the written package fresh from disk (assertion 10).

    Deliberately takes ONLY a path: no database, no orchestrator, no config and
    no snapshot is in scope here, so the artifact must stand on its own.
    """
    manifest = _read_json(package_dir / "manifest.json")
    saved = _read_json(package_dir / "dossier.json")
    records = _read_jsonl(package_dir / "evidence.jsonl")

    # The manifest enumerates the whole package.
    assert manifest["files"] == {
        "manifest": "manifest.json",
        "dossier": "dossier.json",
        "markdown": "dossier.md",
        "evidence": "evidence.jsonl",
        "prompt": "prompt.md",
    }

    # The analysis fields the reader needs are all present, self-contained.
    assert saved["domain"] == DOMAIN
    assert saved["name"] == ACCOUNT_NAME
    assert isinstance(saved["identity"], dict) and saved["identity"]
    assert isinstance(saved["signals"]["counts"], dict)
    assert isinstance(saved["signals"]["active"], list)
    assert isinstance(saved["signals"]["expired"], list)
    assert isinstance(saved["needs"], list) and saved["needs"]
    assert isinstance(saved["demands"], list) and saved["demands"]
    assert saved["coverage"]["rows"], "coverage rows must be embedded verbatim"
    assert saved["generated_at"]
    assert saved["evidence_count"] > 0
    assert records, "evidence.jsonl must carry the evidence records"
    assert len(records) == manifest["evidence_count"] == saved["evidence_count"]


# ---------------------------------------------------------------------------
# The golden flow
# ---------------------------------------------------------------------------


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Build the offline fixture and run the REAL master flow over it."""
    # -- config: temp storage, the repo's real config dir -------------------
    cfg = Config.load()
    cfg.config_dir = str(Path(cfg.config_dir).resolve())
    cfg.storage.db_path = str(tmp_path / "signals.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.storage.dossiers_dir = str(tmp_path / "dossiers")
    cfg.http.respect_robots = False
    assert (Path(cfg.config_dir) / "sources.yaml").is_file()
    assert (Path(cfg.config_dir) / "signals.yaml").is_file()

    # -- database: one account, one open job, one stored document -----------
    db = Database(cfg.storage.db_path)
    registry = AccountRegistry(db)
    registry.upsert(Account(domain=DOMAIN, name=ACCOUNT_NAME), source="e2e")
    assert registry.get(DOMAIN) is not None

    store = RawStore(db, cfg.storage.raw_dir)
    doc = store.put(
        source="company_feed",
        url=FEED_URL,
        body=FEED_BODY.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        status=200,
        domain=DOMAIN,
    )

    db.upsert(
        "jobs",
        {
            "job_key": JOB_KEY,
            "domain": DOMAIN,
            "source": "e2e_fixture",
            "title": JOB_TITLE,
            "department": JOB_DEPARTMENT,
            "url": JOB_URL,
            "description": JOB_DESCRIPTION,
            "closed_at": None,
        },
        pk="job_key",
    )

    # -- the offline orchestrator: LOCAL adapters only ----------------------
    # No network-capable adapter is ever in the list, so collect() can never
    # plan an HTTP task.
    orch = Orchestrator(cfg, db=db, adapters=[JobSignalsSource(), NeedsSource()])
    assert [a.key for a in orch._adapters] == ["jobsignals", "needs"]

    # -- stub the network-facing resolver; record it, never fetch -----------
    resolve_calls: list[dict] = []

    def fake_resolve(self, **kwargs):
        resolve_calls.append(kwargs)
        return {
            "accounts": 1,
            "cik": 0,
            "ats": 0,
            "feeds": 0,
            "icp": 0,
            "g2": 0,
            "appstore": 0,
            "bbb": 0,
            "linkedin": 0,
        }

    monkeypatch.setattr(Orchestrator, "resolve", fake_resolve)

    def fail_find_careers(self, domain):  # pragma: no cover - must never run
        raise AssertionError("find_careers must never be called by the intel flow")

    monkeypatch.setattr(Orchestrator, "find_careers", fail_find_careers)

    # -- stub the profile loader: the shipped default profile is empty ------
    profile = _synthetic_profile()
    profile_calls: list = []

    def fake_load_profile(profile_id, path=None):
        profile_calls.append((profile_id, path))
        return profile

    monkeypatch.setattr(intel, "load_market_profile", fake_load_profile)

    result = intel.run_intel(DOMAIN, config=cfg, orch=orch)

    return SimpleNamespace(
        cfg=cfg,
        db=db,
        registry=registry,
        store=store,
        doc=doc,
        orch=orch,
        profile=profile,
        result=result,
        resolve_calls=resolve_calls,
        profile_calls=profile_calls,
    )


def test_offline_intel_master_flow_end_to_end(flow):
    result = flow.result
    dossier = result["dossier"]

    # -- 1. the five stages ran, in order, none failed ----------------------
    stages = result["stages"]
    assert list(stages) == list(intel.STAGES)
    assert list(stages) == ["identity", "collect", "derive", "score", "package"]
    assert [stages[name]["status"] for name in intel.STAGES] == ["ran"] * 5
    assert result["errors"] == {}

    # -- 2. the resolver was called exactly once; find_careers never called --
    assert len(flow.resolve_calls) == 1
    # Reaching this line at all proves find_careers was not called: its stub
    # raises AssertionError on invocation.
    assert [call[0] for call in flow.profile_calls] == [intel.DEFAULT_MARKET_PROFILE_ID]

    # -- 3. default opt-ins: marketplaces off, no LinkedIn resolution -------
    coverage_rows = result["coverage"]
    by_source = {row["source"]: row for row in coverage_rows}
    assert len(by_source) == len(coverage_rows), "coverage rows must be unique per source"
    for key in intel.MARKETPLACE_SOURCE_KEYS:
        status = by_source[key]["status"]
        assert status not in ("ran_data", "ran_empty"), (
            f"marketplace {key} reported {status!r} without --with-marketplaces"
        )
    assert flow.resolve_calls[0]["g2"] is False
    assert flow.resolve_calls[0]["linkedin"] is False
    assert flow.registry.get(DOMAIN).linkedin_slug is None

    # -- 4. coverage enumerates the config registry exactly once ------------
    configured = _configured_source_keys(flow.cfg.config_dir)
    assert configured, "config/sources.yaml must define sources"
    assert set(by_source) == configured
    assert len(coverage_rows) == len(configured)

    # -- 5. the matched need appears verbatim, with its doc_id/url/offering --
    matched_needs = [n for n in dossier["needs"] if n["quote"] == NEED_SENTENCE]
    assert len(matched_needs) == 1, dossier["needs"]
    need = matched_needs[0]
    assert need["doc_id"] == flow.doc.doc_id
    assert need["url"] == FEED_URL
    assert need["source"] == "company_feed"
    assert need["offering_id"] == OFFERING_ID
    assert SERVED_PHRASE in " ".join(need["match_reasons"])

    # -- 6. the required-stack demand appears with job_key/phrase/vendor ----
    matched_demands = [d for d in dossier["demands"] if d["job_key"] == JOB_KEY]
    assert len(matched_demands) == 1, dossier["demands"]
    demand = matched_demands[0]
    assert demand["vendor"] == VENDOR
    assert demand["phrase"] == "Snowflake"
    assert demand["phrase"] in JOB_DESCRIPTION
    assert demand["department"] == JOB_DEPARTMENT
    assert demand["job_url"] == JOB_URL
    assert demand["offering_id"] == OFFERING_ID

    # -- 7. the unrelated kitchen sentence is nowhere ----------------------
    assert KITCHEN_SENTENCE not in json.dumps(dossier["needs"])
    package_dir = Path(result["paths"]["package_dir"])
    evidence_text = (package_dir / "evidence.jsonl").read_text(encoding="utf-8")
    assert KITCHEN_SENTENCE not in evidence_text
    for record in _read_jsonl(package_dir / "evidence.jsonl"):
        assert KITCHEN_SENTENCE not in (record.get("detail") or "")
        assert "kitchen" not in (record.get("detail") or "").casefold()

    # -- 8. never an absence claim, never a missing_* key -------------------
    for name in ("dossier.json", "dossier.md", "evidence.jsonl"):
        text = (package_dir / name).read_text(encoding="utf-8").casefold()
        for token in _ABSENCE_SUBSTRINGS:
            assert token not in text, f"{token!r} leaked into {name}"
    # No key named like "missing_tool" may appear anywhere in the analysis.
    # One documented exception, and it is the ONLY one: ``coverage.summary`` is
    # a status -> count map keyed by the coverage vocabulary
    # (src/intel/coverage.py:34). Its "missing_requires" key reports that a
    # configured source did not run THIS pass because the account lacked a
    # required field -- a statement about this run, never a claim that a tool
    # or fact does not exist. It is allowed there and nowhere else.
    analysis = {key: value for key, value in dossier.items() if key != "coverage"}
    scanned = [
        *list(_walk_keys(analysis)),
        *list(_walk_keys(dossier["coverage"]["rows"])),
        *list(_walk_keys(_read_jsonl(package_dir / "evidence.jsonl"))),
        *list(_walk_keys(dossier["coverage"]["summary"])),
    ]
    missing_keys = {str(key) for key in scanned if "missing" in str(key).casefold()}
    assert missing_keys <= {"missing_requires"}, (
        f"absence-shaped keys present: {sorted(missing_keys)}"
    )
    assert (dossier["coverage"]["summary"].get("missing_requires") or 0) >= 0
    # "missing_requires" never appears in the record-level analysis.
    record_keys = [
        *list(_walk_keys(analysis)),
        *list(_walk_keys(dossier["coverage"]["rows"])),
        *list(_walk_keys(_read_jsonl(package_dir / "evidence.jsonl"))),
    ]
    assert not [key for key in record_keys if "missing" in str(key).casefold()]

    # -- 9. every referenced evidence_id resolves exactly once -------------
    referenced: list = []
    _collect_evidence_ids(dossier, referenced)
    assert referenced, "the dossier carries no evidence ids at all"
    counts: dict = {}
    for record in _read_jsonl(package_dir / "evidence.jsonl"):
        counts[record["evidence_id"]] = counts.get(record["evidence_id"], 0) + 1
    for evidence_id in referenced:
        assert counts.get(evidence_id) == 1, (
            f"{evidence_id} resolved {counts.get(evidence_id)} times"
        )

    # -- 10. the package is self-contained ---------------------------------
    _assert_self_contained(package_dir)
    for name in PACKAGE_FILES:
        assert (package_dir / name).is_file()

    # -- 11. the prompt demands citations and "unknown" over guessing ------
    prompt = (package_dir / "prompt.md").read_text(encoding="utf-8")
    lowered = prompt.lower()
    assert "unknown" in lowered
    assert "rather than guessing" in lowered
    assert "evidence_id" in lowered
    assert "signal_id" in lowered

    # -- 12. nonzero evidence, internally consistent signal counts ---------
    assert dossier["evidence_count"] > 0
    saved = _read_json(package_dir / "dossier.json")
    assert saved["signals"]["counts"]["active"] == len(saved["signals"]["active"])
    assert saved["signals"]["counts"]["expired"] == len(saved["signals"]["expired"])
    assert saved["signals"]["counts"]["active_total"] >= saved["signals"]["counts"]["active"]
    assert saved["signals"]["counts"]["active"] >= 2  # one need + one demand

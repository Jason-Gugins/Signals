"""Task 14 — entity resolution hardening tests.

Covers:
- normalize_entity table: 'Acme, Inc.' / 'acme inc' / 'The ACME corp' all equal
- fuzzy_match threshold boundary at 0.87 (difflib SequenceMatcher)
- entity_aliases migration v4 (table created + user_version bump)
- registry round-trip: add_entity_alias + resolve by entity alias
- alias lookup wins over fuzzy; normalized match precedes fuzzy fallback
- resolve-time seeding from config/lists/entity_aliases.yaml (idempotent,
  fail-open on a missing file)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.identity.registry import AccountRegistry
from src.identity.resolve import fuzzy_match, normalize_entity
from src.pipeline.orchestrator import Orchestrator


# ── normalization table ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "variant",
    ["Acme, Inc.", "acme inc", "The ACME corp", "ACME Corp.", "acme, llc", "ACME Co."],
)
def test_normalize_entity_variants_all_equal(variant: str) -> None:
    assert normalize_entity(variant) == normalize_entity("Acme Inc")
    assert normalize_entity(variant) == "acme"


def test_normalize_entity_strips_punctuation_and_suffixes() -> None:
    assert normalize_entity("Globex, GmbH.") == "globex"
    assert normalize_entity("Initech Ltd") == "initech"
    assert normalize_entity("  Soylent   Co  ") == "soylent"
    assert normalize_entity("the ACME company") == "acme"


def test_normalize_entity_edge_cases() -> None:
    assert normalize_entity(None) == ""
    assert normalize_entity("") == ""
    assert normalize_entity("   ") == ""
    assert normalize_entity("Clean") == "clean"
    # Non-suffix tokens are preserved.
    assert normalize_entity("Washington Mutual Inc") == "washington mutual"


# ── fuzzy threshold boundary ───────────────────────────────────────────────

def test_fuzzy_match_exact_normalized_is_true() -> None:
    assert fuzzy_match("Acme, Inc.", "acme corp")


def test_fuzzy_match_threshold_boundary_at_087() -> None:
    # Find a string pair that brackets the default threshold exactly.
    a = normalize_entity("Acme Corporation")
    close = normalize_entity("Acme Corporationn")  # one extra char
    lo = "Acme Corporation" + "n" * 1
    hi = normalize_entity("Acme Corporatio")

    def ratio(x: str, y: str) -> float:
        import difflib

        return difflib.SequenceMatcher(None, x, y).ratio()

    # Use explicit threshold boundary behavior: >= threshold matches.
    n1, n2 = normalize_entity(lo), normalize_entity(hi)
    r = ratio(n1, n2)
    assert fuzzy_match(lo, hi, threshold=r)  # exactly at threshold -> True
    assert not fuzzy_match(lo, hi, threshold=r + 0.01)  # just above -> False
    # The default boundary itself is respected.
    assert fuzzy_match(a, close) is (ratio(a, close) >= 0.87)


def test_fuzzy_match_below_threshold_is_false() -> None:
    assert not fuzzy_match("Acme Corp", "Zynga Inc")
    assert not fuzzy_match("Acme", "Acme Corporation Worldwide Global Holdings")


def test_fuzzy_match_empty_is_false() -> None:
    assert not fuzzy_match("", "acme")
    assert not fuzzy_match("acme", "")


# ── migration v4: entity_aliases table ─────────────────────────────────────

def test_migration_v4_creates_entity_aliases_and_bumps_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        version = db.one("PRAGMA user_version")["user_version"]
        assert version >= 4
        cols = db.table_columns("entity_aliases")
        assert "alias" in cols and "domain" in cols
        # alias is the primary key: duplicate insert must fail.
        db.execute(
            "INSERT INTO entity_aliases (alias, domain) VALUES ('acme', 'acme.com')"
        )
        with pytest.raises(Exception):
            db.execute(
                "INSERT INTO entity_aliases (alias, domain) VALUES ('acme', 'other.com')"
            )
    finally:
        db.close()


def test_migration_v4_upgrades_old_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = Database(path)
    db.execute("DROP TABLE entity_aliases")
    db.execute("PRAGMA user_version = 3")
    db.close()

    db2 = Database(path)
    try:
        assert "entity_aliases" in db2.table_columns("entity_aliases") or db2.one(
            "SELECT count(*) AS n FROM entity_aliases"
        ) is not None
        assert db2.one("PRAGMA user_version")["user_version"] >= 4
    finally:
        db2.close()


# ── registry round-trip: add_entity_alias + resolve ────────────────────────

def _registry(tmp_path: Path) -> AccountRegistry:
    db = Database(tmp_path / "reg.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme Inc", tier=1, score=80))
    return reg


def test_entity_alias_round_trip(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # "ACME Corp" shares the domain, so this isn't proof of the alias path.
    reg.add_entity_alias("Globex Traders", "acme.com")
    hit = reg.resolve(name="Globex Traders")
    assert hit is not None
    assert hit.domain == "acme.com"


def test_entity_alias_wins_over_fuzzy(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # A second account whose name is fuzzy-close to the alias query but
    # NOT an exact normalized match (so the exact name-alias path misses
    # and only the entity alias vs fuzzy would decide).
    reg.upsert(Account(domain="globex.com", name="Globex Trading Co Worldwide", tier=2, score=40))
    # Manual alias says the queried name belongs to acme.com — the alias
    # must win over fuzzy similarity with globex.com.
    reg.add_entity_alias("Globex Trading", "acme.com")
    hit = reg.resolve(name="Globex Trading")
    assert hit is not None
    assert hit.domain == "acme.com"


def test_config_driven_alias_population(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.load_entity_aliases_from_config({"Weyland-Yutani": "acme.com"})
    hit = reg.resolve(name="Weyland Yutani Corp")
    assert hit is not None
    assert hit.domain == "acme.com"


def test_normalized_match_before_fuzzy(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # A name variant that normalize_entity maps to an existing alias but
    # which name_similarity alone might miss.
    reg.add_alias("Wayne Enterprises", "name", "acme.com", source="manual")
    hit = reg.resolve(name="WAYNE   ENTERPRISES, Inc.")
    assert hit is not None
    assert hit.domain == "acme.com"


def test_resolve_still_misses_unknown_name(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    assert reg.resolve(name="Totally Unrelated Widgets LLC") is None


def test_normalized_name_collision_falls_through(tmp_path):
    """Two name-alias rows that normalize_entity to the same key but point at
    DIFFERENT accounts are ambiguous: _normalized_name must return None (the
    caller falls through to fuzzy) instead of whichever row comes first."""
    from src.core.db import Database
    from src.core.models import Account
    from src.identity.registry import AccountRegistry

    db = Database(tmp_path / "r.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme Corp"))
    reg.upsert(Account(domain="acme-2.com", name="Acme Co"))
    # 'acme' -> acme-2.com (last-write on equal normalize_name) and
    # 'acme incorporated' -> acme.com; BOTH normalize_entity to 'acme'.
    reg.add_alias("Acme Incorporated", "name", "acme.com", source="test")
    assert reg._normalized_name("Acme Holdings") is None  # ambiguous tie
    # An alias that normalizes to a key matching only ONE account resolves.
    # 'Inc/Corp/Co' all collapse to 'acme', so use a distinct token:
    reg.add_alias("Zeta Analytics", "name", "acme.com", source="test")
    assert reg._normalized_name("Zeta Analytics Ltd").domain == "acme.com"


# ── resolve-time seeding from config/lists/entity_aliases.yaml ─────────────

class _NoFetch:
    """Fetcher that 404s everything: the alias-seeding tests stay offline —
    the ATS ladder runs its full budget against dead ends and never verifies."""

    def get(self, task, **kw):
        doc = Document(
            doc_id=task.url[-16:],
            source=task.source,
            url=task.url,
            domain=task.domain,
            body=b"",
            status=404,
        )
        return FetchResult(False, 404, doc, False, None, 1)


def _resolve_orch(tmp_path: Path, *, with_alias_file: bool) -> Orchestrator:
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = str(tmp_path / "config")
    if with_alias_file:
        lists_dir = tmp_path / "config" / "lists"
        lists_dir.mkdir(parents=True, exist_ok=True)
        (lists_dir / "entity_aliases.yaml").write_text(
            '"Abnormal Security": abnormal.ai\n', encoding="utf-8"
        )
    orch = Orchestrator(cfg, fetcher=_NoFetch())
    orch.registry.upsert(Account(domain="acme.com", name="Acme Inc", tier=1, score=80))
    return orch


def _ats_only_kwargs() -> dict:
    return dict(
        ats=True, cik=False, feeds=False, icp=False, g2=False,
        appstore=False, bbb=False, linkedin=False,
    )


def test_resolve_loads_entity_aliases_from_config(tmp_path: Path) -> None:
    """A config/lists/entity_aliases.yaml mapping {alias: domain} populates the
    registry at resolve time — aliases reach the ATS ladder without manual DB
    scripts, and survive DB rebuilds (config is the source of truth). A second
    resolve is idempotent: the alias PK upsert must not duplicate rows."""
    orch = _resolve_orch(tmp_path, with_alias_file=True)
    out = orch.resolve(domains=["acme.com"], **_ats_only_kwargs())
    assert out["accounts"] == 1
    assert orch.registry.entity_aliases_for("abnormal.ai") == ["abnormal security"]
    orch.resolve(domains=["acme.com"], **_ats_only_kwargs())
    assert orch.registry.entity_aliases_for("abnormal.ai") == ["abnormal security"]


def test_resolve_tolerates_missing_entity_aliases_yaml(tmp_path: Path) -> None:
    """No entity_aliases.yaml in the config dir -> resolve still runs (fail-open
    like every identity resolver) and seeds no aliases."""
    orch = _resolve_orch(tmp_path, with_alias_file=False)
    out = orch.resolve(domains=["acme.com"], **_ats_only_kwargs())
    assert out["accounts"] == 1
    assert orch.registry.entity_aliases_for("abnormal.ai") == []


def test_resolve_seeds_entity_aliases_without_ats_stage(tmp_path: Path) -> None:
    """entity_aliases also feed collect-time identity resolution (sec formd /
    bbb / trustradius read _by_entity_alias), so seeding must NOT be gated on
    the ATS stage: resolve(ats=False) still loads entity_aliases.yaml."""
    orch = _resolve_orch(tmp_path, with_alias_file=True)
    out = orch.resolve(
        domains=["acme.com"], ats=False, cik=False, feeds=False, icp=False,
        g2=False, appstore=False, bbb=False, linkedin=False,
    )
    assert out["accounts"] == 1
    assert orch.registry.entity_aliases_for("abnormal.ai") == ["abnormal security"]


def test_resolve_skips_non_string_alias_rows(tmp_path: Path) -> None:
    """Loader guard: rows whose alias or domain is not a string (a YAML
    bool-ish `no` -> False, null, a nested mapping) must be skipped with a
    warning instead of str()-coerced into garbage rows ("False", "None",
    "{'a': 'b'}"); valid rows still load and resolve never raises."""
    orch = _resolve_orch(tmp_path, with_alias_file=False)
    lists_dir = tmp_path / "config" / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    (lists_dir / "entity_aliases.yaml").write_text(
        '"Abnormal Security": abnormal.ai\n'
        "acme: no\n"  # YAML 1.1: bare `no` parses to False, not a string
        '"Null Domain": null\n'
        "nested: {a: b}\n",
        encoding="utf-8",
    )
    out = orch.resolve(domains=["acme.com"], **_ats_only_kwargs())
    assert out["accounts"] == 1
    rows = orch.registry.db.query("SELECT alias, domain FROM entity_aliases")
    assert [(r["alias"], r["domain"]) for r in rows] == [
        ("abnormal security", "abnormal.ai")
    ]
    assert orch.registry.entity_aliases_for("abnormal.ai") == ["abnormal security"]

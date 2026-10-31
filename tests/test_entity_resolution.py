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
from src.identity.names import name_similarity, normalize_name
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


# ── _by_entity_alias: legacy-scan tie guard (plan Task 3) ──────────────────


def _legacy_entity_tie_seed(reg: AccountRegistry) -> None:
    """Seed the legacy state the _by_entity_alias full scan must defend
    against: two DIRECT-SQL entity_aliases rows whose raw stored aliases
    normalize identically but point at DIFFERENT domains.

    add_entity_alias cannot create this today — it stores the
    normalize_entity form and the alias PK upsert-collapses equal keys —
    but rows written before that normalization (or via direct SQL) can
    carry raw aliases exactly like these."""
    reg.upsert(Account(domain="alpha.com", name="Alpha Prime"))
    reg.upsert(Account(domain="beta.com", name="Beta Prime"))
    reg.db.execute(
        "INSERT OR REPLACE INTO entity_aliases (alias, domain)"
        " VALUES ('Acme East', 'alpha.com')"
    )
    reg.db.execute(
        "INSERT OR REPLACE INTO entity_aliases (alias, domain)"
        " VALUES ('ACME EAST', 'beta.com')"
    )


def test_entity_alias_legacy_scan_tie_returns_none(tmp_path: Path) -> None:
    """_by_entity_alias legacy scan: two entity_aliases rows whose STORED
    aliases normalize_entity to the same key but point at DIFFERENT domains
    are ambiguous — must return None (the caller falls through to the next
    arm), NOT whichever row the full-table scan hits first. Same ties->None
    policy _normalized_name documents for name-aliases.

    Reachability: entity_aliases' alias PK prevents NEW collisions
    (add_entity_alias stores the normalize_entity form, so equal keys
    upsert-collapse to one row), but it does NOT stop two legacy raw rows
    like 'Acme East' / 'ACME EAST' written before that normalization or via
    direct SQL — the exact-key SELECT misses both (case-sensitive string
    equality), so only the legacy scan sees them. Plan Task 3 RED."""
    db = Database(tmp_path / "tie.db")
    reg = AccountRegistry(db)
    _legacy_entity_tie_seed(reg)
    # sanity: the stored raw aliases normalize identically...
    assert (
        normalize_entity("Acme East") == normalize_entity("ACME EAST") == "acme east"
    )
    # ...both rows persisted (distinct raw strings, distinct PKs)...
    rows = reg.db.query("SELECT alias, domain FROM entity_aliases")
    assert [(r["alias"], r["domain"]) for r in rows] == [
        ("Acme East", "alpha.com"),
        ("ACME EAST", "beta.com"),
    ]
    # ...and the exact-key fast path misses both (stored strings differ from
    # the query key), so the legacy scan is the only path that can decide.
    assert (
        reg.db.one(
            "SELECT domain FROM entity_aliases WHERE alias = ?", ("acme east",)
        )
        is None
    )
    assert reg._by_entity_alias("acme east") is None  # ambiguous tie -> None
    assert reg.resolve(name="acme east") is None  # falls through, not row #1


# ── entity-alias priority pins (plan Task 3, characterization) ─────────────


def test_entity_alias_exact_key_beats_legacy_scan(tmp_path: Path) -> None:
    """Priority pin: an exact-key entity_aliases hit short-circuits before
    the legacy scan — legacy raw rows that would tie must not poison the
    canonical (normalize_entity-stored) row for the same key."""
    db = Database(tmp_path / "exact.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="alpha.com", name="Alpha Prime"))
    reg.upsert(Account(domain="beta.com", name="Beta Prime"))
    reg.upsert(Account(domain="exact.com", name="Exact Prime"))
    # legacy raw rows that would tie under the scan...
    reg.db.execute(
        "INSERT OR REPLACE INTO entity_aliases (alias, domain)"
        " VALUES ('Acme East', 'alpha.com')"
    )
    reg.db.execute(
        "INSERT OR REPLACE INTO entity_aliases (alias, domain)"
        " VALUES ('ACME EAST', 'beta.com')"
    )
    # ...plus the canonical exact-key row add_entity_alias writes.
    reg.add_entity_alias("Acme East Co", "exact.com")
    rows = reg.db.query("SELECT alias, domain FROM entity_aliases ORDER BY alias")
    assert [(r["alias"], r["domain"]) for r in rows] == [
        ("ACME EAST", "beta.com"),
        ("Acme East", "alpha.com"),
        ("acme east", "exact.com"),
    ]
    hit = reg.resolve(name="acme east")
    assert hit is not None
    assert hit.domain == "exact.com"


def test_name_alias_beats_entity_alias_same_key(tmp_path: Path) -> None:
    """Priority pin: the exact name-alias arm runs BEFORE the entity-alias
    lookup — when the same normalized key points at a DIFFERENT domain via
    account_aliases vs entity_aliases, the name-alias owner wins."""
    db = Database(tmp_path / "prio.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="namealias.com", name="Namealias Prime"))
    reg.upsert(Account(domain="entity.com", name="Entity Prime"))
    reg.add_alias("Acme East", "name", "namealias.com", source="test")
    reg.add_entity_alias("Acme East Co", "entity.com")
    # both stores hold the key 'acme east' — pointing at DIFFERENT domains
    assert (
        reg.db.one(
            "SELECT domain FROM account_aliases"
            " WHERE alias = ? AND alias_kind = 'name'",
            ("acme east",),
        )["domain"]
        == "namealias.com"
    )
    assert (
        reg.db.one(
            "SELECT domain FROM entity_aliases WHERE alias = ?", ("acme east",)
        )["domain"]
        == "entity.com"
    )
    # 'Acme East LLC' normalizes (names stack) onto the name-alias key...
    assert normalize_name("Acme East LLC") == "acme east"
    hit = reg.resolve(name="Acme East LLC")
    assert hit is not None
    assert hit.domain == "namealias.com"  # NOT entity.com


def test_tie_guarded_entity_alias_falls_through_to_normalized_name(
    tmp_path: Path,
) -> None:
    """Priority pin: when the entity-alias legacy scan is tie-guarded to
    None, resolve() continues down the waterfall — the next arm
    (_normalized_name) still resolves via a name-alias row that only
    normalize_entity (not the names-stack key) maps onto the query key."""
    db = Database(tmp_path / "fall1.db")
    reg = AccountRegistry(db)
    _legacy_entity_tie_seed(reg)
    reg.upsert(Account(domain="gamma.com", name="Gamma Prime"))
    # 'incorporated' is the one resolve-suffix the names stack keeps: the
    # exact name-alias arm misses the query, but normalize_entity maps both
    # onto 'acme east'.
    reg.add_alias("Acme East Incorporated", "name", "gamma.com", source="test")
    assert normalize_name("Acme East Co") == "acme east"
    assert normalize_name("Acme East Incorporated") == "acme east incorporated"
    assert normalize_entity("Acme East Incorporated") == "acme east"
    # the tie itself is guarded -> None...
    assert reg._by_entity_alias("Acme East Co") is None
    # ...and resolve falls through to the normalized name-alias arm
    hit = reg.resolve(name="Acme East Co")
    assert hit is not None
    assert hit.domain == "gamma.com"


def test_tie_guarded_entity_alias_falls_through_to_fuzzy(tmp_path: Path) -> None:
    """Priority pin: with NO normalized-equality name-alias either, the
    tie-guarded entity-alias arm still falls through to _fuzzy_name
    (>= 0.86, single surviving domain) — the guard must not swallow the
    whole waterfall."""
    db = Database(tmp_path / "fall2.db")
    reg = AccountRegistry(db)
    _legacy_entity_tie_seed(reg)
    reg.upsert(Account(domain="gamma.com", name="Gamma Prime"))
    # No name-alias normalizes onto 'acme east' under either stack, but the
    # word-order variant 'East Acme' shares the query's exact token set
    # ({acme, east} — name_tokens strips the trailing 'co'), so
    # name_similarity = 1.0 Jaccard >= the 0.86 registry bar.
    reg.add_alias("East Acme", "name", "gamma.com", source="test")
    assert name_similarity("Acme East Co", "East Acme") == pytest.approx(1.0)
    # ...while BOTH deterministic arms miss: the names-stack key differs
    # ('acme east' vs 'east acme') and normalize_entity('east acme')
    # != 'acme east'.
    assert normalize_name("Acme East Co") == "acme east"
    assert normalize_name("East Acme") == "east acme"
    assert normalize_entity("East Acme") == "east acme"
    assert reg._by_entity_alias("Acme East Co") is None  # tie-guarded
    assert reg._normalized_name("Acme East Co") is None  # normalized arm misses
    hit = reg.resolve(name="Acme East Co")
    assert hit is not None
    assert hit.domain == "gamma.com"  # fuzzy arm catches the fall-through


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

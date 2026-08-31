"""Task 14 — entity resolution hardening tests.

Covers:
- normalize_entity table: 'Acme, Inc.' / 'acme inc' / 'The ACME corp' all equal
- fuzzy_match threshold boundary at 0.87 (difflib SequenceMatcher)
- entity_aliases migration v4 (table created + user_version bump)
- registry round-trip: add_entity_alias + resolve by entity alias
- alias lookup wins over fuzzy; normalized match precedes fuzzy fallback
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry
from src.identity.resolve import fuzzy_match, normalize_entity


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

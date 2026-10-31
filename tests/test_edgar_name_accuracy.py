"""Multi-filer generic-name collision contract, end-to-end (offline).

Characterization tests: they pin CURRENT behavior, they do not prescribe it.
Plan: docs/plans/2026-10-30-name-resolution-tests.md Task 2.

Three groups:
  1. match_cik against a collision-heavy company_tickers index (generic
     Acme/Signals/Delta families + exactly one duplicated ticker pair + one
     unambiguous long-tail filer): generic names never guess, exact
     normalized names still attach.
  2. EdgarIdentityResolver.resolve_all over the same index via a FakeFetcher
     (mirrors tests/test_resolver_isolation.py): ambiguous accounts get no
     CIK written anywhere. The resolver's return value is the honest
     per-domain report — {domain: cik | None}; there is no separate stats
     object, so the mapping itself is what gets asserted.
  3. Form D attach safety (mirrors tests/test_formd_identity.py): an issuer
     colliding with two existing accounts must land on a cik##########.edgar
     stub, not silently attach to either. No entity_aliases rows are seeded
     here — that is the legacy-scan path Task 3 covers.

Structural facts these tests document (measured against the code):
  - account_aliases PK is (alias, alias_kind): two accounts whose names
    normalize identically under names.normalize_name collapse to ONE alias
    row and the last write owns it. A real two-row _normalized_name tie
    needs stored keys that DIFFER under normalize_name but collide under
    resolve.normalize_entity — "incorporated" is the only suffix in
    resolve.LEGAL_SUFFIXES that names.LEGAL_SUFFIXES lacks, so it is what
    makes the tie reachable (same construction as
    test_entity_resolution.test_normalized_name_collision_falls_through).
  - resolve()'s exact name-alias key intercepts before _normalized_name, so
    an issuer whose normalize_name equals a stored alias attaches to that
    row's owner deterministically — pinned as-is below.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.core.db import Database
from src.core.models import Account
from src.identity.edgar_ids import (
    EdgarIdentityResolver,
    match_cik,
    parse_company_tickers,
)
from src.identity.names import name_similarity, normalize_name
from src.identity.registry import AccountRegistry
from src.identity.resolve import normalize_entity
from src.sources.sec.formd_identity import attach_form_d_account
from src.sources.sec.parse_formd import parse_form_d


COLLISION_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sec" / "company_tickers_collision.json"
)
FORM_D_FIX = Path(__file__).parent / "fixtures" / "sec" / "form_d_primary_doc.xml"


def _index():
    return parse_company_tickers(COLLISION_FIXTURE.read_bytes())


# --- fakes: mirror tests/test_resolver_isolation.py exactly ------------------


class FakeDoc:
    def __init__(self, body: bytes):
        self.body = body


class FakeResult:
    def __init__(self, body: bytes | None, ok: bool = True):
        self.ok = ok
        self.doc = FakeDoc(body) if body is not None else None


class FakeFetcher:
    def __init__(self, body: bytes | None = None, ok: bool = True):
        self.body = body
        self.ok = ok
        self.tasks: list = []

    def get(self, task):
        self.tasks.append(task)
        return FakeResult(self.body, ok=self.ok)


class FakeRegistry:
    def __init__(self, fail_domain: str | None = None):
        self.upserts: list[tuple[Account, str | None]] = []
        self.fail_domain = fail_domain

    def upsert(self, account: Account, *, source: str | None = None):
        if self.fail_domain and account.domain == self.fail_domain:
            raise ValueError(f"registry write failed for {account.domain}")
        self.upserts.append((account, source))
        return account


# --- 1. match_cik against the collision fixture ------------------------------


def test_collision_fixture_shape():
    rows = _index()
    assert len(rows) == 12
    # distinct CIKs across the board
    ciks = [r["cik"] for r in rows]
    assert len(set(ciks)) == 12
    # exactly one duplicated ticker pair (PHX), everything else unique
    tickers = [r["ticker"] for r in rows]
    dups = sorted({t for t in tickers if tickers.count(t) > 1})
    assert dups == ["PHX"]
    phx = [r for r in rows if r["ticker"] == "PHX"]
    assert len(phx) == 2 and phx[0]["cik"] != phx[1]["cik"]


def test_match_cik_generic_acme_exact_tie_returns_none():
    # "Acme" normalizes to 'acme' — and so do BOTH 'ACME CO' and
    # 'ACME COMPANY' (distinct CIKs). The exact-normalized arm sees a 2-way
    # tie -> None and never reaches the fuzzy arm. Plan Task 2 test 1.
    rows = _index()
    assert normalize_name("ACME CO") == normalize_name("ACME COMPANY") == normalize_name("Acme")
    # even the fuzzy arm would stay conservative (1/3 Jaccard + 0.15 prefix):
    assert name_similarity("Acme", "ACME GLOBAL HOLDINGS INC") < 0.9
    assert match_cik(Account(domain="acme.com", name="Acme"), rows) is None


def test_match_cik_generic_signals_inc_exact_tie_returns_none():
    # 'SIGNALS INC' and 'SIGNALS, INC.' are distinct registrants whose EDGAR
    # titles normalize identically ('signals') -> exact-arm 2-way tie -> None.
    rows = _index()
    assert normalize_name("SIGNALS INC") == normalize_name("SIGNALS, INC.") == "signals"
    assert name_similarity("Signals Inc", "SIGNALS TECHNOLOGY CORP") < 0.9
    assert match_cik(Account(domain="signals.com", name="Signals Inc"), rows) is None


def test_match_cik_generic_delta_fuzzy_miss_returns_none():
    # No row normalizes to bare 'delta', so the fuzzy arm runs and stays
    # conservative: both Delta rows score well under the 0.9 default bar
    # ({delta} vs {delta,air,lines} = 1/3 + 0.15; vs {delta,faucet} = 1/2 + 0.15).
    rows = _index()
    assert name_similarity("Delta", "DELTA AIR LINES INC") < 0.9
    assert name_similarity("Delta", "DELTA FAUCET COMPANY") < 0.9
    assert match_cik(Account(domain="delta.com", name="Delta"), rows) is None


def test_match_cik_exact_normalized_name_still_resolves():
    # The precise name cuts through the generic family via the exact
    # normalized arm (no ticker on the account — name arm only).
    rows = _index()
    assert normalize_name("Delta Air Lines Inc") == normalize_name("DELTA AIR LINES INC")
    assert name_similarity("Delta Air Lines Inc", "DELTA AIR LINES INC") == 1.0
    assert (
        match_cik(Account(domain="delta.com", name="Delta Air Lines Inc"), rows)
        == "0002000010"
    )


def test_match_cik_duplicated_ticker_returns_none_without_name_fallthrough():
    # Two registrants share ticker PHX on distinct CIKs -> the ticker arm
    # returns None immediately; it does NOT fall through to the (here
    # unique) name arm. Contrast shows the name alone would resolve.
    rows = _index()
    acct = Account(domain="phoenix.com", name="Phoenix Logistics Group Inc", ticker="PHX")
    assert match_cik(acct, rows) is None
    assert (
        match_cik(
            Account(domain="phoenix.com", name="Phoenix Logistics Group Inc"), rows
        )
        == "0002100010"
    )


# --- 2. resolve_all end-to-end over the collision index ----------------------


def test_resolve_all_generic_ambiguity_writes_no_cik():
    # resolve_all returns {domain: cik | None} — that mapping IS the pass
    # report: ambiguous accounts map to None, are never upserted, and their
    # Account.cik stays untouched. Plan Task 2 test 2.
    fetcher = FakeFetcher(COLLISION_FIXTURE.read_bytes())
    registry = FakeRegistry()
    resolver = EdgarIdentityResolver(fetcher, registry)
    acme = Account(domain="acme.example", name="Acme")
    delta = Account(domain="delta.example", name="Delta")
    widgetworks = Account(domain="widgetworks.example", name="Widgetworks Analytics Inc")
    signalstech = Account(domain="signalstech.example", name="Signals Technology Corp")
    # sanity: the resolvable names are exact normalized matches (1.0)...
    assert name_similarity("Widgetworks Analytics Inc", "WIDGETWORKS ANALYTICS INC") == 1.0
    assert name_similarity("Signals Technology Corp", "SIGNALS TECHNOLOGY CORP") == 1.0
    out = resolver.resolve_all([acme, delta, widgetworks, signalstech])
    assert out == {
        "acme.example": None,
        "delta.example": None,
        "widgetworks.example": "0002200010",
        "signalstech.example": "0001900010",
    }
    # only the resolved accounts were written, tagged sec_edgar
    assert [u[0].domain for u in registry.upserts] == [
        "widgetworks.example",
        "signalstech.example",
    ]
    assert [u[1] for u in registry.upserts] == ["sec_edgar", "sec_edgar"]
    # ambiguous Account objects were not mutated with a guessed CIK
    assert acme.cik is None and delta.cik is None
    assert widgetworks.cik == "0002200010"
    # one index fetch served the whole pass, and the parsed index is the fixture
    assert len(fetcher.tasks) == 1
    assert resolver._index == _index()


def test_resolve_all_registry_write_failure_isolated_per_account():
    # Per-account isolation arm (edgar_ids.resolve_all's except branch): a
    # registry write failure for ONE account must not kill the CIK pass —
    # the exception is caught, that domain maps to None in the report, and
    # the REST of the cohort still resolves and upserts normally. Exercises
    # the previously-unused FakeRegistry.fail_domain scaffolding.
    fetcher = FakeFetcher(COLLISION_FIXTURE.read_bytes())
    registry = FakeRegistry(fail_domain="widgetworks.example")
    resolver = EdgarIdentityResolver(fetcher, registry)
    broken = Account(domain="widgetworks.example", name="Widgetworks Analytics Inc")
    healthy = Account(domain="signalstech.example", name="Signals Technology Corp")
    # sanity: both names are exact normalized matches — match_cik itself
    # succeeds for the broken account; only its registry WRITE fails.
    assert name_similarity("Widgetworks Analytics Inc", "WIDGETWORKS ANALYTICS INC") == 1.0
    assert name_similarity("Signals Technology Corp", "SIGNALS TECHNOLOGY CORP") == 1.0
    out = resolver.resolve_all([broken, healthy])
    # the failed write is reported honestly as None; the healthy account
    # still got its CIK.
    assert out == {"widgetworks.example": None, "signalstech.example": "0001900010"}
    # only the healthy account was written, tagged sec_edgar
    assert [u[0].domain for u in registry.upserts] == ["signalstech.example"]
    assert [u[1] for u in registry.upserts] == ["sec_edgar"]
    assert healthy.cik == "0001900010"
    # the except arm does NOT roll back the Account: acct.cik was assigned
    # before the failed upsert — the per-domain mapping is the report.
    assert broken.cik == "0002200010"


# --- 3. Form D attach safety -------------------------------------------------


def _reg(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "s.db"))


def _fd(entity_name: str, cik: str):
    # same parsed Form D fixture as tests/test_formd_identity.py, with the
    # issuer identity swapped for the collision scenario
    return replace(
        parse_form_d(FORM_D_FIX.read_bytes()), entity_name=entity_name, cik=cik
    )


def _seed_acme_east_pair(reg: AccountRegistry) -> None:
    # 'Acme East' and 'ACME EAST LLC' normalize identically under
    # names.normalize_name ('acme east'). account_aliases PK is
    # (alias, alias_kind), so the pair collapses to ONE row and the last
    # write owns it. Mirrors test_formd_identity.py's upsert seeding.
    reg.upsert(Account(domain="acmeeast.com", name="Acme East"))
    reg.upsert(Account(domain="acmeeastllc.com", name="ACME EAST LLC"))


def test_formd_attach_exact_normalized_name_attaches_last_write(tmp_path):
    # Current behavior, characterized: 'Acme East Co' normalizes to the
    # stored 'acme east' name-alias key, so resolve() returns that row's
    # single owner (the last-written account) deterministically and the Form
    # D CIK lands there. No entity_aliases rows are involved.
    reg = _reg(tmp_path)
    _seed_acme_east_pair(reg)
    row = reg.db.one(
        "SELECT domain FROM account_aliases WHERE alias = ? AND alias_kind = 'name'",
        ("acme east",),
    )
    assert row["domain"] == "acmeeastllc.com"  # last write owns the shared key
    assert normalize_name("Acme East Co") == normalize_name("ACME EAST LLC") == "acme east"
    got = attach_form_d_account(_fd("Acme East Co", "0001707925"), reg)
    assert got.domain == "acmeeastllc.com"
    assert got.cik == "0001707925"
    assert got.seed_source == "sec_formd"
    assert reg.get("acmeeast.com").cik is None
    assert [r["domain"] for r in reg.db.query("SELECT domain FROM accounts")] == [
        "acmeeast.com",
        "acmeeastllc.com",
    ]


def test_formd_attach_normalized_name_tie_stubs_not_steals(tmp_path):
    # The _normalized_name tie -> None fall-through under the Form D path
    # (formd_identity.attach_form_d_account -> registry.resolve(name=...)):
    # two DIFFERENT name-alias rows that resolve.normalize_entity maps to the
    # same key while pointing at different accounts -> ambiguous -> None ->
    # the issuer becomes a cik##########.edgar stub. No entity_aliases rows
    # are seeded — that is Task 3's legacy-scan path. Plan Task 2 test 3.
    reg = _reg(tmp_path)
    _seed_acme_east_pair(reg)
    # The plan's literal pair collapses to one stored row (PK is
    # (alias, alias_kind)), so seed the second row via add_alias with
    # 'Acme East Incorporated' — 'incorporated' is the only suffix
    # resolve.normalize_entity strips that names.normalize_name keeps, which
    # is what makes a real two-row tie reachable.
    reg.add_alias("Acme East Incorporated", "name", "acmeeast.com", source="test")
    rows = reg.db.query(
        "SELECT alias, domain FROM account_aliases WHERE alias_kind = 'name'"
    )
    assert [(r["alias"], r["domain"]) for r in rows] == [
        ("acme east", "acmeeastllc.com"),
        ("acme east incorporated", "acmeeast.com"),
    ]
    issuer = "Acme East Co., Incorporated"
    # sanity: the issuer entity-normalizes onto the shared tie key...
    assert normalize_entity(issuer) == "acme east"
    # ...but its name-key is stored nowhere, so the exact alias arm misses...
    assert normalize_name(issuer) == "acme east co incorporated"
    # ...and the fuzzy arm stays under the 0.86 registry bar for BOTH rows
    # ({acme,east,co,incorporated} vs {acme,east}: 2/4 + 0.15 = 0.65;
    #  vs {acme,east,incorporated}: 3/4 + 0.08 = 0.83):
    assert name_similarity(issuer, "acme east") == pytest.approx(0.65)
    assert name_similarity(issuer, "acme east incorporated") == pytest.approx(0.83)
    # the tie itself: _normalized_name sees two domains -> None
    assert reg._normalized_name(issuer) is None
    assert reg.resolve(name=issuer) is None
    got = attach_form_d_account(_fd(issuer, "0001707925"), reg)
    assert got.domain == "cik0001707925.edgar"
    assert got.cik == "0001707925"
    assert got.seed_source == "sec_formd"
    # neither existing account was re-pointed or given the CIK
    assert reg.get("acmeeast.com").cik is None
    assert reg.get("acmeeastllc.com").cik is None
    assert sorted(r["domain"] for r in reg.db.query("SELECT domain FROM accounts")) == [
        "acmeeast.com",
        "acmeeastllc.com",
        "cik0001707925.edgar",
    ]

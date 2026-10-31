"""npm registry search parser contract tests (Task 12 / C3).

FIXTURE PROVENANCE — SYNTHETIC: tests/fixtures/community/npm_search.json is
NOT a live capture. Its shape mirrors npm's documented registry search
envelope ({"objects": [{"package": {...}, "downloads": {...}, ...}]}); the
surface it freezes was live-verified 2026-10-30 on alpha-sense.com
(HTTP 200, signals_new=15 through the full collect). Network is blocked by
the autouse guard; everything here is offline and pure.
"""

from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.community.github import github_org_guess
from src.sources.community.npm import (
    npm_delta,
    npm_search_url,
    npm_to_candidates,
    owned_packages,
    parse_npm_search,
)

FIXTURE = Path("tests/fixtures/community/npm_search.json").read_bytes()


def test_url():
    assert npm_search_url("acme corp") == "https://registry.npmjs.org/-/v1/search?text=acme%20corp&size=20"


def test_parse_search_shape():
    pkgs = parse_npm_search(FIXTURE)
    assert pkgs and pkgs[0]["name"] == "@acme/sdk"
    assert pkgs[0]["downloads_monthly"] == 1234
    assert pkgs[0]["repository"] == "git+https://github.com/acmecorp/sdk.git"
    assert parse_npm_search(b'{"objects": []}') == []
    assert parse_npm_search(b"not json") == []


def test_owned_packages_strong_arm():
    acct = Account(domain="acme.com", name="Acme Corp")
    owned = owned_packages(parse_npm_search(FIXTURE), acct)
    # @acme/sdk: repository org "acmecorp" casefolds to a github_org_guess
    # entry -> owned. acme-helpers: name collision, its repository points at
    # widgettools -> never guessed, ignored.
    assert [p["name"] for p in owned] == ["@acme/sdk"]


def test_owned_packages_org_charset_edge():
    """rstrip(".git") strips the CHARSET {.g i t}, not the SUFFIX: org
    logins ending in i/t/g were mangled ("intuit" -> "intu", "metaig" ->
    "meta"). The org segment never legitimately carries ".git" (it is the
    path BEFORE the repo name), so the suffix must be removed with
    removesuffix only.

    Both directions are pinned:
    - Intuit account (guess "intuit"): github.com/intuit/sdk.git MUST own
      (the mangled "intu" matched no guess -> false negative before).
    - Metaig account (guesses "metaig"): github.com/metaig/app.git MUST own
      (mangled to "meta", matched nothing), while the real Meta org's
      github.com/meta/other must NOT be owned — and, mirror side, a Meta
      account (guess "meta") must NOT own metaig's package via the mangled
      match (the false positive the charset strip actually created)."""
    intuit = Account(name="Intuit", domain="intuit.com")
    assert github_org_guess(intuit) == ["intuit"]
    intuit_pkgs = [{"name": "quickbooks-sdk",
                    "repository": "git+https://github.com/intuit/sdk.git"}]
    assert [p["name"] for p in owned_packages(intuit_pkgs, intuit)] == ["quickbooks-sdk"]

    metaig = Account(name="Metaig", domain="metaig.com")
    assert set(github_org_guess(metaig)) == {"metaig"}
    metaig_own = [{"name": "metaig-app", "repository": "https://github.com/metaig/app.git"}]
    assert [p["name"] for p in owned_packages(metaig_own, metaig)] == ["metaig-app"]
    # The real Meta org's package is not the Metaig account's, either way.
    meta_pkgs = [{"name": "other-tool", "repository": "https://github.com/meta/other"}]
    assert owned_packages(meta_pkgs, metaig) == []
    # False-positive direction the mangle created: Meta account's guesses
    # contain "meta"; "metaig" mangled to "meta" would own the lookalike.
    meta = Account(name="Meta", domain="meta.com")
    assert "meta" in github_org_guess(meta)
    assert owned_packages(metaig_own, meta) == []


def test_brand_mention_emits_intent():
    acct = Account(domain="acme.com", name="Acme Corp")
    pkgs = [{"name": "@thirdparty/acme-toolkit", "description": "Unofficial MCP server for the Acme platform",
             "downloads_monthly": 128, "repository": "git+https://github.com/other/toolkit.git"}]
    cands = npm_to_candidates(pkgs, acct, today=date(2026, 10, 30))
    assert len(cands) == 1
    c = cands[0]
    assert c.signal_type == "intent_3rd_topic"
    assert c.natural_key == "npm:@thirdparty/acme-toolkit"
    assert c.confidence == 0.4
    assert c.evidence_data["downloads_monthly"] == 128


def test_no_mention_no_signal_and_pure_guard():
    acct = Account(domain="acme.com", name="Acme Corp")
    pkgs = [{"name": "@other/thing", "description": "unrelated", "downloads_monthly": 0}]
    assert npm_to_candidates(pkgs, acct, today=date(2026, 10, 30)) == []


def test_adapter_plan_and_harvest():
    from src.sources.community.collector import NpmSource
    src = NpmSource()
    acct = Account(domain="acme.com", name="Acme Corp")
    tasks = src.plan(acct, None)
    assert len(tasks) == 1 and "registry.npmjs.org" in tasks[0].url and tasks[0].domain == "acme.com"
    # plan guard: neither name nor domain -> no tasks (plan() is pure).
    assert src.plan(Account(domain=""), None) == []
    # harvest_packages (runner hook, harvest_repos precedent): owned packages
    # for the momentum diff; [] without a body.
    doc = type("D", (), {"body": FIXTURE})()
    assert [p["name"] for p in src.harvest_packages(doc, acct, {})] == ["@acme/sdk"]
    assert src.harvest_packages(type("D", (), {"body": b""})(), acct, {}) == []
    # parse: brand-mention intents only — the OWN package never surfaces here
    # (it is Task 12's momentum input via harvest_packages).
    cands = src.parse(doc, acct, {"today": "2026-10-30"})
    assert [c.natural_key for c in cands] == ["npm:acme-helpers"]


# ── Task 12 (C3): package momentum (new_package / downloads_surge) ──────────


def test_npm_delta_new_package_window():
    prev = {}
    pkgs = [{"name": "@acme/sdk", "date": "2026-10-20T00:00:00.000Z", "downloads_monthly": 5}]
    out = npm_delta(prev, pkgs, today=date(2026, 10, 30))
    assert out == [("new_package", {"name": "@acme/sdk", "version": None, "published": "2026-10-20"})]


def test_npm_delta_old_package_silent():
    prev = {}
    pkgs = [{"name": "@acme/sdk", "date": "2021-01-01T00:00:00.000Z", "downloads_monthly": 5}]
    assert npm_delta(prev, pkgs, today=date(2026, 10, 30)) == []


def test_npm_delta_downloads_surge():
    prev = {"@acme/sdk": {"version": "1.0.0", "downloads_monthly": 200, "seen_at": "2026-09-01"}}
    pkgs = [{"name": "@acme/sdk", "date": "2026-10-01T00:00:00.000Z", "downloads_monthly": 400}]
    kinds = [k for k, _ in npm_delta(prev, pkgs, today=date(2026, 10, 30))]
    assert kinds == ["downloads_surge"]


def test_npm_delta_surge_needs_floor():
    prev = {"@acme/sdk": {"version": "1.0.0", "downloads_monthly": 10, "seen_at": "2026-09-01"}}
    pkgs = [{"name": "@acme/sdk", "date": "2026-10-01T00:00:00.000Z", "downloads_monthly": 40}]
    assert npm_delta(prev, pkgs, today=date(2026, 10, 30)) == []


# ── sibling-parser tolerance + fallback pins (review pass 2026-10-31) ───────


def test_parse_search_non_dict_body_is_empty():
    """Tolerance parity with the sibling parsers: any parseable-but-wrong
    shape degrades to [], never raises. A top-level list (b'[1,2]') raised
    AttributeError off raw.get before the guard."""
    assert parse_npm_search(b"[1,2]") == []
    assert parse_npm_search(b'"just a string"') == []
    assert parse_npm_search(b"null") == []


def test_parse_search_uncoercible_downloads_degrade_to_zero():
    """The monthly/weekly counters coerce through int(); a registry anomaly
    (or a mutated fixture) must degrade to 0, not raise."""
    body = (b'{"objects": [{"package": {"name": "@acme/sdk", "version": "1.0.0"},'
            b'"downloads": {"monthly": "lots", "weekly": [1, 2]}}]}')
    pkgs = parse_npm_search(body)
    assert len(pkgs) == 1
    assert pkgs[0]["downloads_monthly"] == 0
    assert pkgs[0]["downloads_weekly"] == 0
    # The healthy fields survive the degraded counters.
    assert pkgs[0]["name"] == "@acme/sdk"


def test_npm_delta_garbage_date_falls_back_fresh():
    """Mirror of repo_delta's pinned fallback: no parseable publish date
    means the package is new to us either way -> emit new_package with
    published=None (a garbage date must never suppress the emission)."""
    out = npm_delta({}, [{"name": "@acme/sdk", "date": "not-a-date", "downloads_monthly": 5}],
                    today=date(2026, 10, 30))
    assert out == [("new_package", {"name": "@acme/sdk", "version": None, "published": None})]

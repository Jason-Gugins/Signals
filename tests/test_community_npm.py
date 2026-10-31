from datetime import date
from pathlib import Path

from src.core.models import Account
from src.sources.community.npm import (
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

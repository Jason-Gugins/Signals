from pathlib import Path

from src.core.models import Account
from src.sources.community.npm import npm_search_url, owned_packages, parse_npm_search

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

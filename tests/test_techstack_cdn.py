"""npm-CDN version evidence extractor — fixture-frozen contract tests."""
from pathlib import Path

from src.sources.techstack.cdn import extract_npm_cdn_refs

FIXTURE = Path("tests/fixtures/techstack/cdn_refs.html").read_text(encoding="utf-8")


def test_jsdelivr_unscoped():
    refs = extract_npm_cdn_refs('<script src="https://cdn.jsdelivr.net/npm/some-sdk@1.71.0/dist/index.js">')
    assert refs == [{"package": "some-sdk", "version": "1.71.0",
                     "url": "cdn.jsdelivr.net/npm/some-sdk@1.71.0/dist/index.js"}]


def test_jsdelivr_scoped():
    refs = extract_npm_cdn_refs("https://cdn.jsdelivr.net/npm/@vendor/analytics@2.3.9/dist/x.min.js")
    assert refs[0]["package"] == "@vendor/analytics"
    assert refs[0]["version"] == "2.3.9"


def test_unpkg_and_versionless():
    html = ('<script src="https://unpkg.com/other-sdk@3.1.4/lib.js"></script>'
            '<script src="https://unpkg.com/plain-package/main.js"></script>')
    refs = extract_npm_cdn_refs(html)
    by_pkg = {r["package"]: r for r in refs}
    assert by_pkg["other-sdk"]["version"] == "3.1.4"
    assert by_pkg["plain-package"]["version"] is None


def test_latest_tag_is_not_a_version():
    refs = extract_npm_cdn_refs("https://cdn.jsdelivr.net/npm/some-sdk@latest/x.js")
    assert refs[0]["version"] is None


def test_dedup_and_noise():
    dup = 'x https://unpkg.com/a@1.0.0/y.js y https://unpkg.com/a@1.0.0/z.js'
    assert len(extract_npm_cdn_refs(dup)) == 1
    assert extract_npm_cdn_refs("no urls here") == []
    assert extract_npm_cdn_refs("") == []
    # gh/ non-npm jsdelivr paths must not match
    assert extract_npm_cdn_refs("https://cdn.jsdelivr.net/gh/user/repo@main/x.js") == []


def test_fixture_end_to_end():
    refs = extract_npm_cdn_refs(FIXTURE)
    pkgs = {r["package"] for r in refs}
    assert {"some-sdk", "@vendor/analytics", "other-sdk", "plain-package"} <= pkgs

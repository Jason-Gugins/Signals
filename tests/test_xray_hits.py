"""Profile/company extraction: slug regex, title@company parse, domain normalize.

PURE extractors over the result dicts ({url, title, snippet}) that
src/sources/xray/serp.py:parse_results returns. Root-domain semantics come from
src/identity/domains.py:root_domain (imported, never reimplemented here).
"""
from pathlib import Path

from src.sources.xray.hits import extract_companies, extract_profiles
from src.sources.xray.serp import parse_results

FIX = Path(__file__).parent / "fixtures" / "xray"

R = lambda url, title="", snippet="": {"url": url, "title": title, "snippet": snippet}


def test_profile_slug_extraction():
    hits = extract_profiles([
        R("https://www.linkedin.com/in/jane-doe-12345/", "Jane Doe - Head of Growth",
          "Jane Doe - Head of Growth - Acme Corp | LinkedIn"),
        R("https://de.linkedin.com/in/jane_doe", "Jane Doe", ""),
        R("https://www.linkedin.com/in/", "broken", ""),   # empty slug dropped
    ], string_id="people_title_city")
    assert [h.slug for h in hits] == ["jane-doe-12345", "jane_doe"]
    assert hits[0].string_id == "people_title_city"


def test_title_company_from_serp_title():
    hits = extract_profiles([
        R("https://www.linkedin.com/in/jane-doe/", "Head of Growth at Acme Corp",
          "Head of Growth at Acme Corp · Austin"),
    ], string_id="s")
    assert hits[0].title == "Head of Growth"
    assert hits[0].company == "Acme Corp"


def test_title_with_person_name_prefix():
    # TITLE_AT_RE is greedy on the name prefix; person name stays in title group.
    hits = extract_profiles([
        R("https://www.linkedin.com/in/jane-doe/", "Jane Doe - Head of Growth at Acme Corp",
          ""),
    ], string_id="s")
    assert hits[0].company == "Acme Corp"
    assert "Head of Growth" in hits[0].title


def test_company_hit_normalizes_root_domain_and_drops_known():
    # Known-domain semantics (root via src.identity.domains.root_domain): when
    # acme.com is a known cohort account, EVERY hit whose root is acme.com is
    # dropped — blog.acme.com and www.acme.com alike, and other.acme.com too.
    # A LinkedIn profile URL never becomes a company hit.
    res = [
        R("https://blog.acme.com/we-are-hiring", "Acme is hiring a Head of Sales"),
        R("https://www.acme.com/", "Acme"),
        R("https://other.acme.com/", "Acme subdomain page"),
        R("https://www.linkedin.com/in/x/", "person"),
    ]
    hits = extract_companies(res, string_id="hiring_post_role",
                             known_domains={"acme.com"}, exclude={"linkedin.com"})
    assert hits == []

    # Positive case: an unknown root on a subdomain host is kept, host carried
    # as-is (only a leading www. is stripped), evidence URL and string_id attached.
    hits = extract_companies(
        [R("https://news.newco.io/funding", "NewCo raises seed")],
        string_id="hiring_post_role", known_domains=set(), exclude={"linkedin.com"})
    assert len(hits) == 1
    assert hits[0].domain == "news.newco.io"
    assert hits[0].url == "https://news.newco.io/funding"
    assert hits[0].string_id == "hiring_post_role"


def test_company_hit_dedupes_by_root_domain_first_wins():
    # blog.newco.com and www.newco.com share the root newco.com -> one hit,
    # the FIRST result wins; unrelated roots are unaffected.
    res = [
        R("https://blog.newco.com/we-are-hiring", "NewCo is hiring"),
        R("https://www.newco.com/", "NewCo"),
        R("https://otherco.com/", "OtherCo"),
    ]
    hits = extract_companies(res, string_id="hiring_post_role",
                             known_domains=set(), exclude=set())
    assert [h.domain for h in hits] == ["blog.newco.com", "otherco.com"]
    assert hits[0].url == "https://blog.newco.com/we-are-hiring"


def test_profiles_from_real_ddg_lite_fixture():
    # Parser -> extractor seam, guarded with real captured markup: the Task 4
    # probe fixture goes through the real parse_results, then extraction.
    body = (FIX / "ddg_lite_serp.html").read_text(encoding="utf-8")
    results = parse_results(body, engine="ddg_lite")
    hits = extract_profiles(results, string_id="people_title_city")
    assert len(hits) >= 2
    slugs = {h.slug for h in hits}
    assert "austinheaton" in slugs
    assert all("linkedin.com/in/" in h.url for h in hits)
    assert all(h.string_id == "people_title_city" for h in hits)

from datetime import date
from pathlib import Path

from src.core.models import Account, Contact
from src.sources.content.blog import blog_to_candidates
from src.sources.content.itunes import itunes_to_candidates, itunes_url, parse_itunes
from src.sources.news.feeds import NewsItem

ACCT = Account(domain="acme.com", name="Acme")


def test_itunes():
    assert "itunes.apple.com" in itunes_url("Acme & Co")
    items = parse_itunes((Path("tests/fixtures/content/itunes.json")).read_bytes())
    contacts = [Contact(person_key="jane", name="Jane Doe")]
    cands = itunes_to_candidates(items, ACCT, contacts, today=date(2026, 8, 16))
    assert cands[0].person_key == "jane"


def test_blog_rules():
    items = [
        NewsItem("Introducing Widget", "https://acme.com/a", "2026-08-01", "We launched it", None),
        NewsItem("We're SOC 2 Type II certified", "https://acme.com/b", "2026-08-01", "compliance", None),
        NewsItem("Opening our Toronto office", "https://acme.com/c", "2026-08-01", "geo", None),
        NewsItem("Weekly notes", "https://acme.com/d", "2026-08-01", "standup recap", None),
    ]
    cands = blog_to_candidates(items, ACCT, today=date(2026, 8, 16))
    types = {c.signal_type for c in cands}
    assert "product_launch" in types
    assert "certification" in types
    assert "office_open" in types
    assert all(c.confidence <= 0.85 for c in cands)
    assert len(cands) == 3


def test_itunes_unnormalizable_company_name_does_not_crash():
    # "LLC" normalizes to None (legal-suffix strip empties it) — TypeError'd at
    # `company not in blob_n` before the guard. Post-fix, None maps onto the
    # existing "" convention, and "" means NO company filter (`"" in blob_n` is
    # always True), so the candidate still flows through. Crash-freedom is the
    # contract here, not emptiness.
    account = Account(name="LLC", domain="example.com")
    items = [{"trackName": "Some Product Launch", "trackViewUrl": "u", "releaseDate": "2026-01-02"}]
    out = itunes_to_candidates(items, account, contacts=[], today=date(2026, 11, 6))
    assert len(out) == 1

from datetime import date
from pathlib import Path
from src.core.db import Database
from src.core.models import Account
from src.identity.registry import AccountRegistry
from src.sources.marketplace.trustradius import parse_g2_reviews, parse_trustradius_reviews, reviews_to_candidates
from src.sources.marketplace.capterra import parse_capterra_reviews

HTML = (Path("tests/fixtures/marketplace/trustradius.html")).read_text(encoding="utf-8")


def test_parsers_and_resolution(tmp_path):
    for fn in (parse_trustradius_reviews, parse_g2_reviews, parse_capterra_reviews):
        revs = fn(HTML, "https://trustradius.com/products/acme/reviews")
        assert len(revs) == 3
    db = Database(tmp_path / "s.db")
    reg = AccountRegistry(db)
    reg.upsert(Account(domain="acme.com", name="Acme"))
    pairs = reviews_to_candidates(parse_trustradius_reviews(HTML, "https://x/compare"), reg, today=date(2026, 8, 16), own_product="acme")
    assert pairs and pairs[0][0] == "acme.com"
    assert pairs[0][1].confidence == 0.85
    # old review dropped; unknown unresolved
    assert len(pairs) == 1

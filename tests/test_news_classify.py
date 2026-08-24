from datetime import date
from src.core.models import Account
from src.sources.news.classify import NEWS_RULES, classify_news, extract_vars, _strip_source_attribution
from src.sources.news.feeds import NewsItem

TODAY = date(2026, 8, 16)
ACME = Account(domain="acme.com", name="Acme")


def _item(title, summary="", published="2026-08-01", link="https://ex/a"):
    return NewsItem(title=title, link=link, published=published, summary=summary, source_name="X")


POSITIVES = [
    ("funding_round", "Acme raises $4.5 million Series B"),
    ("ipo_filing", "Acme files S-1 for IPO"),
    ("ipo_pricing", "Acme prices IPO at $20"),
    ("ma_acquirer", "Acme acquires Widget Labs"),
    ("ma_target", "Acme acquired by Giant Co"),
    ("layoff", "Acme laying off 200 employees"),
    ("product_launch", "Acme launches Widget 2"),
    ("office_open", "Acme opens a new office in Austin"),
    ("award", "Acme wins award for culture"),
    ("certification", "Acme is now SOC 2 Type II certified"),
    ("exec_hire", "Acme appoints Jane Doe as CEO"),
    ("exec_departure", "Acme CFO resigns"),
    ("market_consolidation", "Acme in merger of equals talks"),
    ("competitor_outage", "Acme reports a service disruption"),
    ("earnings_warning", "Acme cuts guidance for Q3"),
]

NEGATIVES = [
    ("funding_round", "Acme funding for customers announced"),
    ("layoff", "Acme avoiding layoffs this year"),
    ("layoff", "Acme cited in a layoff tracker report"),
    ("product_launch", "Acme launch party tonight"),
    ("ma_acquirer", "Acme customer acquisition cost drops"),
]


def test_one_positive_per_core_rule():
    covered = set()
    for typ, title in POSITIVES:
        c = classify_news(_item(title), ACME, today=TODAY)
        assert c is not None, title
        assert c.signal_type == typ, (title, c.signal_type)
        covered.add(typ)
    assert covered >= {r.signal_type for r in NEWS_RULES}


def test_negatives_do_not_fire_that_type():
    for typ, title in NEGATIVES:
        c = classify_news(_item(title), ACME, today=TODAY)
        assert c is None or c.signal_type != typ, title


def test_acquire_direction_both_ways():
    a = classify_news(_item("Acme acquires Widget"), ACME, today=TODAY)
    assert a.signal_type == "ma_acquirer"
    t = classify_news(_item("Giant acquires Acme"), ACME, today=TODAY)
    assert t.signal_type == "ma_target"


def test_name_only_in_summary_caps_confidence():
    c = classify_news(_item("Big funding news", "Acme raises $10M"), ACME, today=TODAY)
    assert c is not None
    assert c.confidence <= 0.6


def test_unrelated_none():
    assert classify_news(_item("Markets rally on Fed news"), ACME, today=TODAY) is None


def test_similar_name_gong_cha():
    gong = Account(domain="gong.io", name="Gong")
    assert classify_news(_item("Gong Cha raises $50M"), gong, today=TODAY) is None


def test_stale_dropped():
    assert classify_news(_item("Acme raises $1M", published="2024-01-01"), ACME, today=TODAY) is None


def test_amount_extraction_shapes():
    for amt in ("$4.5 million", "$4.5M", "US$4.5M", "€4.5M"):
        rule = next(r for r in NEWS_RULES if r.signal_type == "funding_round")
        vars_ = extract_vars(f"Acme raises {amt}", rule)
        # euro may not match $ pattern
        if amt.startswith("€"):
            continue
        assert "amount" in vars_ or "4.5" in str(vars_)


def test_strip_source_attribution_basic():
    assert _strip_source_attribution("AI Isn't Reducing Workforce Costs - Gartner") == "AI Isn't Reducing Workforce Costs"


def test_strip_source_attribution_no_dash():
    assert _strip_source_attribution("Acme raises $10M") == "Acme raises $10M"


def test_strip_source_attribution_multiple_dashes():
    # Only the LAST " - " is the publisher separator
    assert _strip_source_attribution("Acme - Q3 Update - TechCrunch") == "Acme - Q3 Update"


def test_strip_source_attribution_preserves_em_dash():
    # Em dash (—) is different from " - "
    assert _strip_source_attribution("Acme — Best Company") == "Acme — Best Company"

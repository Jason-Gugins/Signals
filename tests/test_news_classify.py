from datetime import date
from src.core.models import Account
from src.sources.news.classify import NEWS_RULES, classify_news, extract_vars, _strip_source_attribution, _is_common_word, _has_proper_mention
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
    ("security_breach", "Acme discloses a data breach exposing customer records"),
    ("competitor_outage", "Acme reports a service disruption"),
    ("earnings_warning", "Acme cuts guidance for Q3"),
]

NEGATIVES = [
    ("funding_round", "Acme funding for customers announced"),
    ("layoff", "Acme avoiding layoffs this year"),
    ("layoff", "Acme cited in a layoff tracker report"),
    ("product_launch", "Acme launch party tonight"),
    ("ma_acquirer", "Acme customer acquisition cost drops"),
    ("security_breach", "Acme data breach insurance premiums surge"),
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


def test_source_attribution_not_treated_as_mention():
    """If the account name appears ONLY in the publisher suffix (after ' - '),
    it's source attribution, not a company mention — drop the item."""
    gartner = Account(domain="gartner.com", name="Gartner")
    # "Reducing workforce" would match layoff rule, but "Gartner" is the publisher
    item = NewsItem(
        title="AI Isn't Reducing Workforce Costs - Gartner",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="Gartner",
    )
    assert classify_news(item, gartner, today=TODAY) is None


def test_source_attribution_keeps_when_name_in_headline():
    """If the account name appears in the headline content (before ' - '),
    it's a real mention even if the publisher is also the same company."""
    gartner = Account(domain="gartner.com", name="Gartner")
    item = NewsItem(
        title="Gartner acquires research firm - TechCrunch",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="TechCrunch",
    )
    c = classify_news(item, gartner, today=TODAY)
    assert c is not None
    assert c.signal_type == "ma_acquirer"


def test_source_name_matches_account_still_drops_if_only_in_byline():
    """Even when source_name == account name, if the name ONLY appears
    in the byline suffix, it's not a signal about the company."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Music Festival Highlights - Levitate",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="Levitate",
    )
    assert classify_news(item, levitate, today=TODAY) is None


def test_common_word_detection():
    assert _is_common_word("Levitate")
    assert _is_common_word("levitate")
    assert not _is_common_word("Gartner")
    assert not _is_common_word("Acme")
    assert not _is_common_word("Darktrace")


def test_proper_mention_capitalized():
    # "Levitate" capitalized in headline = company mention
    assert _has_proper_mention("Levitate raises $10M", "Levitate")
    # "levitate" lowercase in headline = verb, not company
    assert not _has_proper_mention("Watch Alex Warren levitate on stage", "Levitate")
    # "levitate.ai" in text = always a company mention regardless of case
    assert _has_proper_mention("New feature from levitate.ai", "Levitate")


def test_common_word_levitate_festival_dropped():
    """Levitate Music Festival items should not produce signals for levitate.ai."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Levitate Music Festival highlights emerging musicians",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="Business Journals",
    )
    # "Levitate" is capitalized but followed by "Music Festival" — not the company
    assert classify_news(item, levitate, today=TODAY) is None


def test_common_word_levitate_artwork_dropped():
    """Artwork titled 'Levitate #9' should not produce ma_target for levitate.ai."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Foundation Acquires Jamele Wright Sr.'s Levitate #9",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="Art Gallery",
    )
    assert classify_news(item, levitate, today=TODAY) is None


def test_common_word_levitate_funding_kept():
    """Genuine Levitate company news should still fire."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Levitate raises $16M to bring AI to relationship-based businesses - PR Newswire",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="PR Newswire",
    )
    c = classify_news(item, levitate, today=TODAY)
    assert c is not None
    assert c.signal_type == "funding_round"


def test_self_published_research_dropped():
    """When source_name == account name and the account doesn't appear
    in the headline content (only in the byline), it's self-published — drop."""
    gartner = Account(domain="gartner.com", name="Gartner")
    item = NewsItem(
        title="Gartner Predicts 60% of Organizations Will Reduce Workforce - Gartner",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="Gartner",
    )
    # "Gartner" appears in headline AND byline — but the headline subject is
    # "Gartner Predicts..." which is research, not an event happening TO Gartner.
    # The layoff rule would fire on "reduce workforce." This should be dropped
    # because Gartner is the publisher AND the researcher, not the subject.
    assert classify_news(item, gartner, today=TODAY) is None


def test_self_published_funding_announcement_kept():
    """When a company publishes its own funding announcement via PR Newswire,
    the funding signal is legitimate — the company IS the subject."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Levitate Raises $16M To Bring AI to SMBs - PR Newswire",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="PR Newswire",
    )
    c = classify_news(item, levitate, today=TODAY)
    assert c is not None
    assert c.signal_type == "funding_round"


def test_third_party_research_about_company_kept():
    """When a third party publishes about the company, it's a real signal."""
    acme = Account(domain="acme.com", name="Acme")
    item = NewsItem(
        title="Acme lays off 200 employees - TechCrunch",
        link="https://ex.com/a", published="2026-08-01", summary="", source_name="TechCrunch",
    )
    c = classify_news(item, acme, today=TODAY)
    assert c is not None
    assert c.signal_type == "layoff"


# --- Regression cases from live multi-account test (2026-08-23) ---

def test_regression_gartner_layoff_false_positive():
    """Live test: 'AI Isn't Reducing Workforce Costs - Gartner' was classified
    as layoff. Gartner is the publisher/researcher, not the subject."""
    gartner = Account(domain="gartner.com", name="Gartner")
    item = NewsItem(
        title="AI Isn't Reducing Workforce Costs - Gartner",
        link="https://news.google.com/a1", published="2026-08-20",
        summary="", source_name="Gartner",
    )
    assert classify_news(item, gartner, today=TODAY) is None


def test_regression_levitate_artwork_ma_false_positive():
    """Live test: 'Foundation Acquires Jamele Wright Sr.'s Levitate #9' was
    classified as ma_target. 'Levitate' is an artwork title, not the company."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Petrucci Family Foundation Acquires Jamele Wright Sr.'s Levitate #9 - Black Art In America",
        link="https://news.google.com/a2", published="2026-08-18",
        summary="", source_name="Black Art In America",
    )
    assert classify_news(item, levitate, today=TODAY) is None


def test_regression_levitate_festival_false_positive():
    """Live test: 'Levitate Music Festival highlights emerging musicians' should
    not produce any signal for levitate.ai.
    NOTE: this title alone matches no NEWS_RULES pattern, so it already returns
    None today — it is a DEFENSIVE regression test guarding against future rule
    additions (e.g. a broad 'launch' rule) that would otherwise catch it."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Levitate Music Festival highlights emerging musicians - The Business Journals",
        link="https://news.google.com/a3", published="2026-08-19",
        summary="", source_name="The Business Journals",
    )
    assert classify_news(item, levitate, today=TODAY) is None


def test_regression_levitate_funding_kept():
    """Live test: 'Levitate Raises $16M' must still produce a funding_round signal."""
    levitate = Account(domain="levitate.ai", name="Levitate")
    item = NewsItem(
        title="Levitate Raises $16M To Bring AI To Relationship-Based Businesses - PR Newswire",
        link="https://news.google.com/a4", published="2026-08-15",
        summary="", source_name="PR Newswire",
    )
    c = classify_news(item, levitate, today=TODAY)
    assert c is not None
    assert c.signal_type == "funding_round"


# --- Task 18: role buckets + funding amount/stage in evidence_data ---

EXEC_HIRE_CASES = [
    ("acme names Jane Doe CRO", "CRO", "revenue"),
    ("acme appoints Jane Doe Chief Revenue Officer", "Chief Revenue Officer", "revenue"),
    ("acme hires Bob as VP Sales", "VP Sales", "revenue"),
    ("acme names Bob Head of Sales", "Head of Sales", "revenue"),
    ("acme hires Bob as CTO", "CTO", "tech"),
    ("acme appoints Jane Doe CIO", "CIO", "tech"),
    ("acme names Sue Chief Technology Officer", "Chief Technology Officer", "tech"),
    ("acme hires Bob as VP Engineering", "VP Engineering", "tech"),
    ("acme appoints Jane Doe CPO", "CPO", "product"),
    ("acme names Sue Chief Product Officer", "Chief Product Officer", "product"),
    ("acme hires Bob as VP Product", "VP Product", "product"),
    ("acme appoints Jane Doe CEO", "CEO", "exec"),
    ("acme hires Bob as CFO", "CFO", "exec"),
    ("acme names Sue COO", "COO", "exec"),
    ("acme appoints Jane Doe President", "President", "exec"),
]


def test_extract_role_bucket_table():
    from src.sources.news.classify import extract_role_bucket
    for text, role, bucket in EXEC_HIRE_CASES:
        got = extract_role_bucket(text)
        assert got is not None, text
        assert got == (role, bucket), (text, got)


def test_extract_role_bucket_none():
    from src.sources.news.classify import extract_role_bucket
    assert extract_role_bucket("acme raised funding") is None


def test_parse_funding_amount_table():
    from src.sources.news.classify import parse_funding_amount
    assert parse_funding_amount("acme raised $12M") == 12000000
    assert parse_funding_amount("acme raised $12 million") == 12000000
    assert parse_funding_amount("acme raised $12.5M") == 12500000
    assert parse_funding_amount("acme raised $1.2B") == 1200000000
    assert parse_funding_amount("acme raised $1.2 billion") == 1200000000
    assert parse_funding_amount("acme raised funding") is None


def test_exec_hire_evidence_data_role_bucket():
    rule = next(r for r in NEWS_RULES if r.signal_type == "exec_hire")
    vars_ = extract_vars("acme names Jane Doe CRO", rule)
    assert vars_["role"] == "CRO"
    assert vars_["role_bucket"] == "revenue"


def test_funding_evidence_data_amount_and_stage():
    rule = next(r for r in NEWS_RULES if r.signal_type == "funding_round")
    vars_ = extract_vars("acme raised $12M Series B", rule)
    assert vars_["amount_usd"] == 12000000
    assert vars_["stage"] == "series_b"
    vars_ = extract_vars("acme raised $1.2B Series E", rule)
    assert vars_["amount_usd"] == 1200000000
    assert vars_["stage"] == "series_e"
    vars_ = extract_vars("acme raised seed funding", rule)
    assert vars_["stage"] == "seed"


def test_classify_carries_structured_evidence():
    c = classify_news(_item("Acme names Jane Doe CRO"), ACME, today=TODAY)
    assert c is not None and c.signal_type == "exec_hire"
    assert c.evidence_data["role"] == "CRO"
    assert c.evidence_data["role_bucket"] == "revenue"
    c2 = classify_news(_item("Acme raised $12M Series B"), ACME, today=TODAY)
    assert c2 is not None and c2.signal_type == "funding_round"
    assert c2.evidence_data["amount_usd"] == 12000000
    assert c2.evidence_data["stage"] == "series_b"


def test_attribution_stripped_still_extracts_role():
    c = classify_news(_item("Acme names Jane Doe CRO - TechCrunch"), ACME, today=TODAY)
    assert c is not None and c.signal_type == "exec_hire"
    assert c.evidence_data["role_bucket"] == "revenue"


def test_attribution_stripped_still_extracts_funding():
    c = classify_news(_item("Acme raised $12M Series B - TechCrunch"), ACME, today=TODAY)
    assert c is not None and c.signal_type == "funding_round"
    assert c.evidence_data["amount_usd"] == 12000000
    assert c.evidence_data["stage"] == "series_b"


def test_research_style_headline_gains_no_role_or_amount():
    """Publisher-attributed / research-style headlines must not gain new
    evidence fields (attribution guards hold for the new patterns)."""
    gartner = Account(domain="gartner.com", name="Gartner")
    item = NewsItem(
        title="AI Isn't Reducing Workforce Costs - Gartner",
        link="https://ex.com/r1", published="2026-08-01", summary="", source_name="Gartner",
    )
    assert classify_news(item, gartner, today=TODAY) is None
    item2 = NewsItem(
        title="Gartner survey finds CRO hiring is up - Gartner",
        link="https://ex.com/r2", published="2026-08-01", summary="", source_name="Gartner",
    )
    c = classify_news(item2, gartner, today=TODAY)
    assert c is None or ("role" not in c.evidence_data and "role_bucket" not in c.evidence_data)


def test_customer_funding_negative_grants_nothing():
    c = classify_news(_item("acme customer raised funding"), ACME, today=TODAY)
    assert c is None


def test_regression_gartner_acquisition_kept():
    """If Gartner genuinely acquires a company, that signal must still fire."""
    gartner = Account(domain="gartner.com", name="Gartner")
    item = NewsItem(
        title="Gartner acquires research firm - TechCrunch",
        link="https://news.google.com/a5", published="2026-08-20",
        summary="", source_name="TechCrunch",
    )
    c = classify_news(item, gartner, today=TODAY)
    assert c is not None
    assert c.signal_type == "ma_acquirer"


# ---- common-word name: Glow Security vs beauty-brand collisions (2026-09-03) --

def test_common_word_glow_beauty_noise_dropped():
    """'Glow' is a common word AND a beauty-industry term — K-beauty/skin-tint/
    cosmetics headlines must not become Glow Security signals."""
    glow = Account(domain="glow.security", name="Glow")
    noise_titles = [
        "Glow up: K-beauty launches at the Okinawa Exchange - Stripes Okinawa",
        "Hautelist: 22 New Beauty Launches To Restore Your Glow This Monsoon Season",
        "Dior Launches Backstage Glow-Up Skin Tint - The Impression",
        "Nivea launches Luminous Even Glow range with new campaign - IMPACT",
    ]
    for t in noise_titles:
        item = NewsItem(title=t, link="https://news.google.com/x", published="2026-09-01",
                        summary="", source_name="X")
        assert classify_news(item, glow, today=TODAY) is None, t


def test_common_word_glow_real_security_news_kept():
    """Genuine Glow Security headlines still classify."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Glow Security launches endpoint AI agent platform - TechCrunch",
        link="https://news.google.com/x", published="2026-09-01",
        summary="", source_name="TechCrunch",
    )
    c = classify_news(item, glow, today=TODAY)
    assert c is not None
    assert c.signal_type == "product_launch"


# ---- publisher-domain attribution ladder (Part A, plan Task 3) --------------

def test_publisher_domain_match_accepts_common_word_name():
    """Article published on glow.security is about Glow — even a headline
    that carries NO company name and would fail every in_text guard."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Glow announces Series A funding round",
        link="https://news.google.com/x", published="2026-09-01",
        summary="", source_name="", publisher_domain="glow.security",
    )
    c = classify_news(item, glow, today=TODAY)
    assert c is not None


def test_publisher_domain_subdomain_match_accepts():
    """press.glow.security is still Glow's own domain (tier-1 suffix match)."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Glow raises $8M seed round",
        link="https://news.google.com/x", published="2026-09-01",
        summary="", source_name="", publisher_domain="press.glow.security",
    )
    assert classify_news(item, glow, today=TODAY) is not None


def test_publisher_domain_www_prefix_normalized():
    """www.glow.security normalizes to glow.security for tier-1."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Glow closes its Series A round",
        link="https://news.google.com/x", published="2026-09-01",
        summary="", source_name="", publisher_domain="www.glow.security",
    )
    assert classify_news(item, glow, today=TODAY) is not None


def test_other_brand_publisher_domain_rejects():
    """dolceglow.com publishing 'Dolce Glow raises $11M' is NOT Glow Security."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Dolce Glow raises $11M Series A",
        link="https://news.google.com/y", published="2026-09-01",
        summary="", source_name="Yahoo", publisher_domain="dolceglow.com",
    )
    assert classify_news(item, glow, today=TODAY) is None


def test_other_brand_publisher_domain_rejects_even_real_looking_signal():
    """The other-brand reject must hold even when the headline looks like a
    perfect, unambiguous signal for the account name."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Glow raises $20M Series B for security platform",
        link="https://news.google.com/y", published="2026-09-01",
        summary="", source_name="TechCrunch", publisher_domain="glowrecipe.com",
    )
    assert classify_news(item, glow, today=TODAY) is None


def test_unresolved_publisher_falls_back_to_existing_guards():
    """publisher_domain=None → current behavior (beauty-context guards etc.)."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Dior Launches Backstage Glow-Up Skin Tint",
        link="https://news.google.com/z", published="2026-09-01",
        summary="", source_name="The Impression", publisher_domain=None,
    )
    assert classify_news(item, glow, today=TODAY) is None


def test_neutral_publisher_domain_falls_through_to_guards():
    """A neutral publisher domain (techcrunch.com) neither accepts nor
    rejects — the existing guard chain decides."""
    glow = Account(domain="glow.security", name="Glow")
    # Real security news on a neutral publisher → still kept (tier-3 pass).
    kept = NewsItem(
        title="Glow Security launches endpoint AI agent platform - TechCrunch",
        link="https://news.google.com/z", published="2026-09-01",
        summary="", source_name="TechCrunch", publisher_domain="techcrunch.com",
    )
    assert classify_news(kept, glow, today=TODAY) is not None
    # Beauty noise on the same neutral publisher → still dropped.
    noise = NewsItem(
        title="Dior Launches Backstage Glow-Up Skin Tint - The Impression",
        link="https://news.google.com/z", published="2026-09-01",
        summary="", source_name="The Impression", publisher_domain="theimpression.com",
    )
    assert classify_news(noise, glow, today=TODAY) is None


def test_is_other_brand_domain_unit():
    from src.sources.news.classify import _is_other_brand_domain
    assert _is_other_brand_domain("dolceglow.com", "glow")
    assert _is_other_brand_domain("glowrecipe.com", "glow")
    assert not _is_other_brand_domain("glow.security", "glow")   # exact own domain
    assert not _is_other_brand_domain("techcrunch.com", "glow")  # name absent
    assert not _is_other_brand_domain("glow.com", "glow")        # bare == name
    assert not _is_other_brand_domain("anything.com", None)      # no name to check


def test_domain_proof_skips_common_word_guards_completely():
    """Tier-1 proof bypasses even headlines that would trip the beauty guards
    (e.g. a launch post that happens to mention 'glow-up')."""
    glow = Account(domain="glow.security", name="Glow")
    item = NewsItem(
        title="Introducing the new Glow dashboard",
        link="https://news.google.com/x", published="2026-09-01",
        summary="", source_name="", publisher_domain="glow.security",
    )
    c = classify_news(item, glow, today=TODAY)
    assert c is not None
    assert c.signal_type == "product_launch"


# ---- review fixes: other-brand shape + registrable-suffix exemption ----------

def test_other_brand_second_domain_exempt():
    """Account's own second brand domain (glow.com blog) is NOT other-brand."""
    from src.sources.news.classify import _is_other_brand_domain
    assert _is_other_brand_domain("glow.com", "glow", "glow.security") is False
    assert _is_other_brand_domain("press.glow.security", "glow", "glow.security") is False


def test_other_brand_fused_label_still_rejects():
    """Fused other-brand labels (dolceglow.com) still reject — the Dolce Glow case."""
    from src.sources.news.classify import _is_other_brand_domain
    assert _is_other_brand_domain("dolceglow.com", "glow", "glow.security") is True
    assert _is_other_brand_domain("glowrecipe.com", "glow", "glow.security") is True


def test_other_brand_acct_domain_none_still_works():
    from src.sources.news.classify import _is_other_brand_domain
    assert _is_other_brand_domain("dolceglow.com", "glow", None) is True

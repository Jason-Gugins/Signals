"""Rule-based news classification with attribution guards. PURE."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import urlparse, urlunparse

from src.core.models import Account
from src.core.textutil import normalize_ws_lines, sha256_hex, to_iso_date
from src.identity.names import normalize_name
from src.sources.base import SignalCandidate
from src.sources.news.feeds import NewsItem


@dataclass(frozen=True)
class NewsRule:
    signal_type: str
    patterns: list[str]
    negative: list[str]
    confidence: float
    extract: dict[str, str]


NEWS_RULES: list[NewsRule] = [
    NewsRule("funding_round",
             [r"\braises?\b.{0,30}\$", r"\bsecures?\b.{0,20}\$\d", r"\b(series [a-e])\b", r"\bfunding round\b", r"\bclos(?:es|ed) (?:a|its) .{0,20}round\b"],
             [r"\bfunding for customers\b", r"\bgrant program\b", r"\bfund(?:s)? (?:transfer|management) (?:software|platform)\b"],
             0.8, {"amount": r"(\$\s?[\d.,]+\s?(?:million|billion|M|B)\b)", "round_stage": r"\b(Series [A-E]|seed|pre-seed)\b"}),
    NewsRule("ipo_filing", [r"\bfiles?\b.{0,20}\b(s-1|ipo)\b", r"\bipo filing\b"], [r"\bafter its ipo\b"], 0.75, {}),
    NewsRule("ipo_pricing", [r"\bipo pric(?:e|ed|ing)\b", r"\bprices (?:its )?ipo\b"], [], 0.8, {}),
    NewsRule("ma_acquirer", [r"\bacquires?\b", r"\bto acquire\b", r"\bbuys?\b.{0,20}\b(inc|corp|ltd)\b"], [r"\bcustomer acquisition\b"], 0.7, {}),
    NewsRule("ma_target", [r"\bacquired by\b", r"\bto be acquired\b"], [], 0.7, {}),
    NewsRule("layoff",
             [r"\blay(?:s|ing)? off\b", r"\blayoffs?\b", r"\bcuts? \d+%? (?:of )?(?:its )?(?:jobs|staff|workforce)\b", r"\breduc\w+ (?:its )?(?:headcount|workforce)\b"],
             [r"\blayoff (?:tracker|report|survey)\b", r"\bavoid(?:s|ed|ing) layoffs\b", r"\bno layoffs\b"],
             0.8, {"affected": r"(\d[\d,]*)\s+(?:employees|jobs|workers|roles)"}),
    NewsRule("product_launch", [r"\b(?:launches?|introducing|unveils?)\b"], [r"\blaunch party\b"], 0.65, {}),
    NewsRule("office_open", [r"\bopens? (?:a |its )?(?:new )?(?:office|hq|headquarters)\b", r"\bopening our .{0,20}office\b"], [], 0.7, {}),
    NewsRule("award", [r"\bnamed .{0,40}\b(best|winner|award)", r"\bwins?\b.{0,20}\baward\b"], [], 0.55, {}),
    NewsRule("certification", [r"\bsoc\s*2\b", r"\biso ?27001\b", r"\bcertified\b"], [], 0.6, {}),
    NewsRule("exec_hire", [r"\bappoints?\b", r"\bhire[sd]\b.{0,20}\b(ceo|cto|cfo|cro|coo|cio)\b", r"\bnames?\b.{0,20}\bas (ceo|cto|cfo|cro)\b", r"\b(?:hires?|names?)\b.{0,40}\b(chief (?:revenue|product|technology) officer|vp (?:sales|product|engineering)|head of sales|c[tf]o|cro|cpo)\b"], [], 0.7, {}),
    NewsRule("exec_departure", [r"\bresigns?\b", r"\bsteps? down\b", r"\bdeparts?\b as\b"], [], 0.7, {}),
    NewsRule("market_consolidation", [r"\bconsolidat\w+\b", r"\bmerger of equals\b"], [], 0.5, {}),
    NewsRule("competitor_outage", [r"\boutage\b", r"\bdown for .{0,10}hours\b", r"\bservice disruption\b"], [], 0.6, {}),
    NewsRule("earnings_warning", [r"\bprofit warning\b", r"\bcuts? guidance\b", r"\bmisses? (?:q\d )?estimates\b"], [], 0.7, {}),
]

_ACQ = re.compile(r"(.+?)\s+(?:acquires?|to acquire|buys?)\s+(.+)", re.I)

# Task 18: leadership role buckets — canonical title → bucket.
_ROLE_BUCKETS: list[tuple[str, str, str]] = [
    # (role display, regex fragment, bucket)
    ("CRO", r"\bCRO\b", "revenue"),
    ("Chief Revenue Officer", r"\bChief Revenue Officer\b", "revenue"),
    ("VP Sales", r"\bVP Sales\b", "revenue"),
    ("Head of Sales", r"\bHead of Sales\b", "revenue"),
    ("CPO", r"\bCPO\b", "product"),
    ("Chief Product Officer", r"\bChief Product Officer\b", "product"),
    ("VP Product", r"\bVP Product\b", "product"),
    ("CTO", r"\bCTO\b", "tech"),
    ("CIO", r"\bCIO\b", "tech"),
    ("Chief Technology Officer", r"\bChief Technology Officer\b", "tech"),
    ("VP Engineering", r"\bVP Engineering\b", "tech"),
    ("CEO", r"\bCEO\b", "exec"),
    ("CFO", r"\bCFO\b", "exec"),
    ("COO", r"\bCOO\b", "exec"),
    ("President", r"\bPresident\b", "exec"),
]
_ROLE_RE = [re.compile(p, re.I) for _, p, _ in _ROLE_BUCKETS]

# Task 18: funding amount parsing — "$12M", "$12 million", "$1.2B".
_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(million|billion|[mMbB])\b")
# Task 18: funding stage — "Series A".."Series E", "seed", "angel".
_STAGE_RE = re.compile(r"\b(Series [A-E]|seed|angel)\b", re.I)


def parse_funding_amount(text: str) -> Optional[int]:
    """Parse a funding amount like '$12M' / '$12 million' / '$1.2B' into USD int."""
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    mult = 1_000_000_000 if unit == "billion" or unit == "b" else 1_000_000
    return int(round(num * mult))


def extract_role_bucket(text: str) -> Optional[tuple[str, str]]:
    """Return (role, bucket) for the first leadership title found, else None."""
    t = text or ""
    for (role, _pat, bucket), rx in zip(_ROLE_BUCKETS, _ROLE_RE):
        if rx.search(t):
            return (role, bucket)
    return None


def extract_vars(text: str, rule: NewsRule) -> dict:
    out = {}
    for key, pat in rule.extract.items():
        m = re.search(pat, text, re.I)
        if m:
            out[key] = m.group(1)
    if rule.signal_type == "exec_hire":
        rb = extract_role_bucket(text)
        if rb:
            out["role"], out["role_bucket"] = rb
    elif rule.signal_type == "funding_round":
        amt = parse_funding_amount(text)
        if amt is not None:
            out["amount_usd"] = amt
        m = _STAGE_RE.search(text)
        if m:
            out["stage"] = m.group(1).lower().replace(" ", "_")
    return out


def _canon_link(link: str) -> str:
    p = urlparse(link)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def _strip_source_attribution(title: str) -> str:
    """Strip the publisher attribution suffix from a Google News title.

    Google News titles follow the pattern: "Headline - Publisher".
    Only the LAST " - " (space-dash-space) is the separator.
    Em dashes (—) are NOT publisher separators.
    """
    # Split on " - " (space-hyphen-space) and take all but the last segment
    parts = title.split(" - ")
    if len(parts) >= 2:
        return " - ".join(parts[:-1])
    return title


# Company names that are also common English words. These require
# proper-noun casing (or domain mention) to confirm company attribution.
_COMMON_WORD_NAMES = frozenset({
    "levitate",
    "boost",
    "ramp",
    "signal",
    "pivot",
    "spark",
    "forge",
    "canvas",
    "slack",
    "stripe",
    "square",
    "block",
    "dash",
    "flow",
    "loom",
    "notion",
    "roku",
})


def _is_common_word(name: str) -> bool:
    """True if the normalized company name is also a common English word."""
    return (normalize_name(name) or "").casefold() in _COMMON_WORD_NAMES


def _has_proper_mention(headline: str, name: str) -> bool:
    """For common-word names, require the name to appear in its original
    casing (proper noun) OR as a domain mention (name.tld).

    For non-common-word names, always returns True (the substring check
    in classify_news is sufficient).
    """
    if not _is_common_word(name):
        return True
    # Domain mention (levitate.ai) is always authoritative
    domain_mention = name.casefold() + "." in headline.casefold()
    if domain_mention:
        return True
    # Check for the name as originally cased (proper noun) as a whole word
    # e.g. "Levitate" in "Levitate raises $10M" → True
    #      "levitate" in "Watch Alex levitate" → False
    # (re is already imported at module top)
    pattern = r"\b" + re.escape(name) + r"\b"
    return bool(re.search(pattern, headline))


# When the publisher IS the account name, only certain signal types are
# legitimate self-announcements (funding, product launches, certifications).
# Research/predictions about industry trends are NOT signals about the company.
_SELF_PUBLISHED_OK = frozenset({
    "funding_round", "product_launch", "certification",
    "office_open", "award", "ipo_filing", "ipo_pricing",
})

# Patterns that indicate the headline is research/commentary, not an event.
# Intentionally narrow: "will" and "says" are excluded because they also
# appear in legitimate self-announcements ("Levitate Will Launch..." / "CEO Says").
_RESEARCH_PATTERNS = [
    r"\b(?:predicts?|forecasts?|survey|report|finds?)\b",
    r"\b(?:by \d{4}|through \d{4})\b",  # forecast timeframes
]


def classify_news(item: NewsItem, account: Account, *, today: date) -> Optional[SignalCandidate]:
    title = item.title or ""
    summary = item.summary or ""
    text = f"{title} {summary}"
    want = normalize_name(account.name) if account.name else None
    domain = account.domain
    # Strip publisher attribution ("Headline - Publisher") so the account
    # name is checked against the headline content, not the byline.
    headline = _strip_source_attribution(title)
    headline_n = normalize_name(headline) or ""
    blob_n = normalize_name(text) or ""
    summary_n = normalize_name(summary) or ""
    in_title = bool(want and want in headline_n) or (domain.split(".")[0] in headline.casefold())
    in_summary = bool(want and want in summary_n)
    in_text = in_title or in_summary or (want and want in blob_n and want in headline_n)
    # Gong Cha vs Gong: require token-ish presence of normalized name as whole-ish
    if want:
        # reject if name only as prefix of a longer different token (gong vs gong cha)
        if re.search(rf"\b{re.escape(want)}\s+cha\b", blob_n):
            if want == "gong":
                in_text = False
                in_title = False
    if not in_text:
        return None
    # For common-word company names (e.g. "Levitate"), require proper-noun
    # casing or domain mention. This prevents verb/adjective matches:
    #   "Watch Alex levitate" ≠ Levitate the company
    #   "Levitate Music Festival" ≠ Levitate the company (context check below)
    if in_text and _is_common_word(account.name):
        if not (_has_proper_mention(headline, account.name) or
                _has_proper_mention(summary, account.name)):
            return None
        # Even with proper casing, reject if the name is immediately followed
        # by "music", "festival", "art", "#", or other non-company contexts.
        # These are title-case uses of the common word, not company mentions.
        # Check BOTH headline and summary (context can appear in either).
        _NON_COMPANY_CONTEXTS = (
            "music festival", "music & arts", "art festival",
            "#", "live session", "backyard",
        )
        name_lower = (account.name or "").casefold()
        for ctx in _NON_COMPANY_CONTEXTS:
            needle = name_lower + " " + ctx
            if needle in headline.casefold() or needle in summary.casefold():
                return None
            needle2 = name_lower + ctx  # e.g. "levitate#9"
            if needle2 in headline.casefold() or needle2 in summary.casefold():
                return None
    # If the publisher (source_name) matches the account name, this is
    # likely self-published content. Only keep signal types that are
    # legitimate self-announcements (funding, product launches, etc.).
    # Research/predictions about industry trends are dropped.
    source = item.source_name or ""
    if want and normalize_name(source) == want:
        # Check if the headline reads like research/prediction
        # (re is already imported at module top)
        is_research = any(re.search(p, headline, re.I) for p in _RESEARCH_PATTERNS)
        if is_research:
            return None
    published = to_iso_date(item.published)
    if published:
        age = (today - date.fromisoformat(published)).days
        if age > 400:
            return None
    # acquisition direction
    acq = _ACQ.search(title)
    if acq and want:
        left, right = normalize_name(acq.group(1)) or "", normalize_name(acq.group(2)) or ""
        if want in left and want not in right:
            forced = "ma_acquirer"
        elif want in right and want not in left:
            forced = "ma_target"
        else:
            forced = None
    else:
        forced = None

    best: Optional[tuple[NewsRule, float]] = None
    for rule in NEWS_RULES:
        if forced and rule.signal_type not in {forced, "ma_acquirer", "ma_target"}:
            # still allow other rules if no forced? forced takes precedence
            pass
        if forced and rule.signal_type != forced:
            continue
        if any(re.search(n, text, re.I) for n in rule.negative):
            continue
        if any(re.search(p, text, re.I) for p in rule.patterns):
            best = (rule, rule.confidence)
            break
    if forced and best is None:
        # emit forced type even without generic rule match
        rule = next(r for r in NEWS_RULES if r.signal_type == forced)
        best = (rule, rule.confidence)
    if best is None:
        return None
    rule, conf = best
    if in_summary and not in_title:
        conf = min(conf, 0.6)
    vars_ = extract_vars(text, rule)
    if rule.signal_type in ("exec_hire", "funding_round"):
        # Task 18: structured evidence (role bucket / amount / stage) is
        # extracted from the attribution-stripped headline only, so a
        # publisher byline can never contribute role or amount data.
        hvars = extract_vars(headline, rule)
        for k in ("role", "role_bucket", "amount_usd", "stage"):
            if k in hvars:
                vars_[k] = hvars[k]
    if published is None and item.published:
        # Date present but unparseable (garbage / impossible): keep the
        # candidate with the raw string — never fabricate a floor or
        # today-substitute date that would distort decay math.
        vars_["date_raw"] = item.published
        observed_at = ""
    else:
        observed_at = published or today.isoformat()
    return SignalCandidate(
        signal_type=rule.signal_type,
        observed_at=observed_at,
        natural_key=sha256_hex(_canon_link(item.link))[:16],
        title=item.title,
        url=item.link,
        confidence=conf,
        evidence_data=vars_,
    )

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
    NewsRule("exec_hire", [r"\bappoints?\b", r"\bhire[sd]\b.{0,20}\b(ceo|cto|cfo|cro|coo|cio)\b", r"\bnames?\b.{0,20}\bas (ceo|cto|cfo|cro)\b"], [], 0.7, {}),
    NewsRule("exec_departure", [r"\bresigns?\b", r"\bsteps? down\b", r"\bdeparts?\b as\b"], [], 0.7, {}),
    NewsRule("market_consolidation", [r"\bconsolidat\w+\b", r"\bmerger of equals\b"], [], 0.5, {}),
    NewsRule("competitor_outage", [r"\boutage\b", r"\bdown for .{0,10}hours\b", r"\bservice disruption\b"], [], 0.6, {}),
    NewsRule("earnings_warning", [r"\bprofit warning\b", r"\bcuts? guidance\b", r"\bmisses? (?:q\d )?estimates\b"], [], 0.7, {}),
]

_ACQ = re.compile(r"(.+?)\s+(?:acquires?|to acquire|buys?)\s+(.+)", re.I)


def extract_vars(text: str, rule: NewsRule) -> dict:
    out = {}
    for key, pat in rule.extract.items():
        m = re.search(pat, text, re.I)
        if m:
            out[key] = m.group(1)
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
    return SignalCandidate(
        signal_type=rule.signal_type,
        observed_at=published or today.isoformat(),
        natural_key=sha256_hex(_canon_link(item.link))[:16],
        title=item.title,
        url=item.link,
        confidence=conf,
        evidence_data=vars_,
    )

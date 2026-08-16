"""Pure text, date, money, and title-classification helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional


_WS = re.compile(r"[\s\u00a0]+")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_YMD_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_YMD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MDY = re.compile(
    r"^(?P<mon>[A-Za-z]+)\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})$"
)
_DMY = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]+)\.?,?\s+(?P<year>\d{4})$"
)
_RELATIVE = re.compile(
    r"^(?:(?P<n>\d+)\s+(?P<unit>days?|weeks?|months?)\s+ago|yesterday|today|last\s+week)$",
    re.I,
)
_MONEY = re.compile(
    r"(?P<cur>CA\$|US\$|C\$|A\$|AU\$|£|€|\$)\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<suf>bn|billion|million|mm|b|m|k)?",
    re.I,
)
_COUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)")

# Ported from ../Linkedin/src/triggers.py
_LEAD = re.compile(
    r"(?:"
    r"\b(?:chief|ceo|cfo|coo|cto|cmo|cro|cio|ciso|cpo)\b"
    r"|\bvice\s+president\b|\bvp\b"
    r"|\bmanaging\s+director\b|\bdirector\b"
    r"|\bhead\s+of\b"
    r"|\bpresident\b|\bfounder\b|\bco-founder\b"
    r")",
    re.I,
)
_NOT_LEAD = re.compile(
    r"\b("
    r"account\s+executive|executive\s+assistant|executive\s+recruiter|"
    r"art\s+director|creative\s+director|associate\s+director|"
    r"assistant\s+director|director\s+of\s+photography"
    r")\b",
    re.I,
)
_C_LEVEL = re.compile(
    r"\b(?:chief\b|ceo|cfo|coo|cto|cmo|cro|cio|ciso|cpo|founder|co-founder|president)\b",
    re.I,
)
_VP = re.compile(r"\b(?:svp|evp|avp|vice\s+president|\bvp)\b", re.I)
_HEAD = re.compile(r"\bhead\s+of\b", re.I)
_DIRECTOR = re.compile(r"\bdirector\b", re.I)
_MANAGER = re.compile(r"\b(?:manager|mgr)\b", re.I)

_DEPTS: list[tuple[str, re.Pattern]] = [
    ("sales", re.compile(r"\b(?:sales|revenue|account\s+exec|ae\b|sdr|bdr|go.?to.?market|gtm)\b", re.I)),
    ("marketing", re.compile(r"\b(?:marketing|demand\s+gen|growth|brand|content\s+market)\b", re.I)),
    ("engineering", re.compile(r"\b(?:engineer|developer|swe|software|devops|sre)\b", re.I)),
    ("finance", re.compile(r"\b(?:finance|controller|accountant|cfo|treasury|fp&a)\b", re.I)),
    ("hr", re.compile(r"\b(?:people|human\s+resources|\bhr\b|recruiter|talent)\b", re.I)),
    ("ops", re.compile(r"\b(?:operations|\bops\b|\bcoo\b|chief\s+operating)\b", re.I)),
    ("product", re.compile(r"\b(?:product\b|cpo|pm\b)\b", re.I)),
    ("support", re.compile(r"\b(?:support|success|csm|customer\s+care)\b", re.I)),
    ("legal", re.compile(r"\b(?:counsel|legal|attorney|compliance)\b", re.I)),
    ("it", re.compile(r"\b(?:\bit\b|information\s+technology|sysadmin|administrator)\b", re.I)),
    ("data", re.compile(r"\b(?:data|analytics|scientist|bi\b)\b", re.I)),
]


def clean_text(s: str | None) -> str | None:
    if s is None:
        return None
    out = _WS.sub(" ", s).strip()
    return out or None


def _from_ymd(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def to_iso_date(value, *, today: date | None = None) -> str | None:
    """Accepts several date shapes. Relative forms require `today`."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        if value >= 1_000_000_000:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    rel = _RELATIVE.match(s)
    if rel:
        if today is None:
            return None
        if re.match(r"today$", s, re.I):
            return today.isoformat()
        if re.match(r"yesterday$", s, re.I):
            return (today - timedelta(days=1)).isoformat()
        if re.match(r"last\s+week$", s, re.I):
            return (today - timedelta(days=7)).isoformat()
        n = int(rel.group("n"))
        unit = rel.group("unit").lower()
        if unit.startswith("day"):
            delta = timedelta(days=n)
        elif unit.startswith("week"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=n * 30)
        return (today - delta).isoformat()

    m = _ISO.match(s)
    if m:
        return _from_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _YMD_SLASH.match(s)
    if m:
        return _from_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"\d{8}", s):
        m = _YMD_COMPACT.match(s)
        return _from_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"\d{9,12}", s):
        return datetime.fromtimestamp(int(s), tz=timezone.utc).date().isoformat()

    m = _MDY.match(s) or _DMY.match(s)
    if m:
        mon = _MONTHS.get(m.group("mon").lower())
        if mon:
            return _from_ymd(int(m.group("year")), mon, int(m.group("day")))
    return None


def days_between(a: str, b: str) -> int | None:
    da = to_iso_date(a)
    db = to_iso_date(b)
    if not da or not db:
        return None
    return (date.fromisoformat(db) - date.fromisoformat(da)).days


def parse_money(s: str | None) -> tuple[float, str] | None:
    if not s:
        return None
    m = _MONEY.search(s)
    if not m:
        return None
    raw = m.group("num").replace(",", "")
    amount = float(raw)
    suf = (m.group("suf") or "").lower()
    mult = {
        "k": 1_000.0,
        "m": 1_000_000.0,
        "mm": 1_000_000.0,
        "million": 1_000_000.0,
        "b": 1_000_000_000.0,
        "bn": 1_000_000_000.0,
        "billion": 1_000_000_000.0,
    }.get(suf, 1.0)
    amount *= mult
    token = m.group("cur")
    currency = {
        "$": "USD",
        "US$": "USD",
        "CA$": "CAD",
        "C$": "CAD",
        "A$": "AUD",
        "AU$": "AUD",
        "€": "EUR",
        "£": "GBP",
    }.get(token, "USD")
    return (amount, currency)


def parse_count(s: str | None) -> int | None:
    if not s:
        return None
    m = _COUNT.search(s)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def normalize_ws_lines(s: str) -> list[str]:
    lines = []
    for line in s.splitlines():
        cleaned = clean_text(line)
        if cleaned:
            lines.append(cleaned)
    return lines


def first_match(patterns: list[re.Pattern], text: str) -> re.Match | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m
    return None


def truncate(s: str | None, n: int = 280) -> str | None:
    if s is None:
        return None
    if len(s) <= n:
        return s
    if n <= 1:
        return "…"
    cut = s[: n - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "…"


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_id(*parts: str, length: int = 16) -> str:
    return sha256_hex("|".join(parts))[:length]


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def guess_seniority(title: str | None) -> str:
    s = (title or "").strip()
    if not s:
        return "unknown"
    if _NOT_LEAD.search(s):
        return "ic"
    if _C_LEVEL.search(s) and not _VP.search(s):
        # "Vice President" contains president — VP already handled
        if re.search(r"\bvice\s+president\b", s, re.I):
            return "vp"
        return "c_level"
    if _VP.search(s):
        return "vp"
    if _HEAD.search(s):
        return "head"
    if _DIRECTOR.search(s):
        return "director"
    if _MANAGER.search(s):
        return "manager"
    return "ic"


def guess_department(title: str | None) -> str | None:
    if not title:
        return None
    for name, pat in _DEPTS:
        if pat.search(title):
            return name
    return None


def guess_persona(title: str | None) -> str:
    if not title:
        return "unknown"
    seniority = guess_seniority(title)
    dept = guess_department(title)
    if seniority == "c_level" or seniority == "vp":
        return "economic_buyer"
    if seniority in {"director", "head"}:
        return "champion"
    if dept in {"engineering", "it", "data"} or seniority == "unknown" and dept == "engineering":
        return "technical"
    if re.search(r"\b(?:engineer|architect|developer|cto)\b", title, re.I):
        return "technical"
    if seniority in {"ic", "manager"}:
        return "user"
    return "unknown"

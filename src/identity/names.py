"""Company-name normalization and fuzzy matching. Pure — no I/O."""

from __future__ import annotations

import re

from src.identity.domains import MULTI_LABEL_TLDS, root_domain


LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "gmbh",
        "sa",
        "srl",
        "bv",
        "ab",
        "oy",
        "plc",
        "pty",
        "co",
        "company",
        "lp",
        "llp",
        "pllc",
        "ag",
        "nv",
        "spa",
        "srl",
        "pte",
    }
)

_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


def normalize_name(s: str | None) -> str | None:
    """casefold, strip legal suffix/punct/'the', collapse whitespace."""
    if s is None:
        return None
    text = s.casefold().strip()
    if not text:
        return None
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in text.split() if t]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens = tokens[:-1]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    out = " ".join(tokens).strip()
    return out or None


def name_tokens(s: str) -> list[str]:
    norm = normalize_name(s)
    return norm.split() if norm else []


def name_similarity(a: str, b: str) -> float:
    """0..1 token-set Jaccard + prefix bonus. Symmetric."""
    ta = set(name_tokens(a))
    tb = set(name_tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    jaccard = len(inter) / len(union)
    na = normalize_name(a) or ""
    nb = normalize_name(b) or ""
    bonus = 0.0
    if na and nb and (na.startswith(nb) or nb.startswith(na)):
        bonus = 0.15
    elif na.split()[:1] == nb.split()[:1]:
        bonus = 0.08
    score = min(1.0, jaccard + bonus)
    if na == nb:
        return 1.0
    return score


def _domain_label(domain: str) -> str:
    root = root_domain(domain) or domain.casefold().removeprefix("www.")
    for tld in sorted(MULTI_LABEL_TLDS, key=len, reverse=True):
        suffix = "." + tld
        if root.endswith(suffix):
            return root[: -len(suffix)]
    if "." in root:
        return root.rsplit(".", 1)[0]
    return root


def name_matches_domain(name: str, domain: str) -> bool:
    """True when the company name and domain clearly refer to the same org.

    Spirit of looks_like_same_company() from ../Linkedin/src/company_text.py.
    """
    tokens = name_tokens(name)
    if not tokens:
        return False
    label = _domain_label(domain).replace("-", "")
    if not label:
        return False
    compact = "".join(tokens)
    if compact == label or compact.replace(" ", "") == label:
        return True
    if tokens[0] == label or label == tokens[0]:
        return True
    if label.startswith(compact) or compact.startswith(label):
        return len(label) >= 3 and len(compact) >= 3
    return False

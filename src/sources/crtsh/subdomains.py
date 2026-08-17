"""crt.sh subdomain parser. Timeouts are a no-op at the collector."""

from __future__ import annotations

import json

from src.sources.techstack.fingerprint import TechMatch


SUBDOMAIN_HINTS = {
    "status": "statuspage",
    "info": "marketing_automation",
    "go": "marketing_automation",
    "jobs": "ats",
    "help": "support",
    "docs": "docs",
    "shop": "ecommerce",
    "app": "product",
}


def parse_crtsh(body: bytes) -> list[str]:
    if not body:
        return []
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return []
    names = set()
    for row in raw if isinstance(raw, list) else []:
        val = row.get("name_value") or ""
        for part in val.replace("\\n", "\n").replace(",", "\n").split("\n"):
            n = part.strip().casefold().lstrip("*.")
            if n:
                names.add(n)
    return sorted(names)


def infer_from_subdomains(subs: list[str], rules: dict) -> list[TechMatch]:
    out = []
    for s in subs:
        label = s.split(".")[0]
        hint = SUBDOMAIN_HINTS.get(label)
        if not hint:
            continue
        out.append(TechMatch(hint, hint, [hint], "mid", f"subdomain:{s}", 0.45))
    return out

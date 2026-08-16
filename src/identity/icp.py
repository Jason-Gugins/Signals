"""ICP fit evaluation from config/icp.yaml rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.core.config import ConfigError
from src.core.models import Account, Signal
from src.identity.lists import load_domain_list


KNOWN_PREDICATES = {
    "employee_count_between",
    "employee_count_lt",
    "employee_count_gt",
    "industry_any",
    "hq_country_any",
    "company_type_any",
    "has_signal_type",
    "department_any",
    "domain_in_file",
    "tech_any",
    "name_regex",
}


@dataclass
class IcpResult:
    multiplier: float
    reasons: list[str]
    disqualified: bool
    disqualify_reason: str | None


def evaluate_icp(
    account: Account,
    rules: dict,
    *,
    signals: list[Signal] | None = None,
) -> IcpResult:
    signals = signals or []
    for rule in rules.get("disqualifiers") or []:
        if _matches(account, rule.get("when") or {}, signals):
            return IcpResult(
                multiplier=0.0,
                reasons=[rule.get("reason") or rule.get("id") or "disqualified"],
                disqualified=True,
                disqualify_reason=rule.get("reason"),
            )
    mult = 1.0
    reasons: list[str] = []
    for rule in rules.get("rules") or []:
        if _matches(account, rule.get("when") or {}, signals):
            mult *= float(rule.get("multiplier", 1.0))
            if rule.get("reason"):
                reasons.append(rule["reason"])
    if mult < 0.0:
        mult = 0.0
    if mult > 2.0:
        mult = 2.0
    return IcpResult(multiplier=mult, reasons=reasons, disqualified=False, disqualify_reason=None)


def _matches(account: Account, when: dict[str, Any], signals: list[Signal]) -> bool:
    unknown = set(when) - KNOWN_PREDICATES
    if unknown:
        raise ConfigError(f"Unknown ICP predicate(s): {sorted(unknown)}")
    if not when:
        return False
    if "employee_count_between" in when:
        lo, hi = when["employee_count_between"]
        if account.employee_count is None or not (lo <= account.employee_count <= hi):
            return False
    if "employee_count_lt" in when:
        if account.employee_count is None or not (account.employee_count < when["employee_count_lt"]):
            return False
    if "employee_count_gt" in when:
        if account.employee_count is None or not (account.employee_count > when["employee_count_gt"]):
            return False
    if "industry_any" in when:
        if not account.industry or account.industry not in when["industry_any"]:
            return False
    if "hq_country_any" in when:
        if not account.hq_country or account.hq_country not in when["hq_country_any"]:
            return False
    if "company_type_any" in when:
        if not account.company_type or account.company_type not in when["company_type_any"]:
            return False
    if "domain_in_file" in when:
        listed = load_domain_list(when["domain_in_file"])
        if account.domain not in listed:
            return False
    if "name_regex" in when:
        if not account.name or not re.search(when["name_regex"], account.name, re.I):
            return False
    if "tech_any" in when:
        techs = set()
        extra = account.extra_data or {}
        for item in extra.get("tech") or extra.get("technologies") or []:
            techs.add(str(item).casefold())
        wanted = {str(t).casefold() for t in when["tech_any"]}
        if not (techs & wanted):
            return False
    if "has_signal_type" in when:
        typ = when["has_signal_type"]
        depts = when.get("department_any")
        hit = False
        for sig in signals:
            if sig.signal_type != typ:
                continue
            if depts:
                dept = (sig.evidence_data or {}).get("department")
                if dept not in depts:
                    continue
            hit = True
            break
        if not hit:
            return False
    elif "department_any" in when:
        raise ConfigError("department_any requires has_signal_type")
    return True

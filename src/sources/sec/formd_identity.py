"""Attach a parsed Form D issuer to the account registry."""

from __future__ import annotations

from src.core.models import Account
from src.identity.edgar_ids import pad_cik
from src.sources.sec.parse_formd import FormD


def stub_domain(cik: str) -> str:
    return f"cik{pad_cik(cik)}.edgar"


def attach_form_d_account(fd: FormD, registry, *, cohort: str | None = "formd") -> Account:
    cik10 = pad_cik(fd.cik)
    hit = registry.resolve(cik=cik10)
    if hit is None and fd.entity_name:
        hit = registry.resolve(name=fd.entity_name)
        if hit is not None and not hit.cik:
            hit.cik = cik10
            return registry.upsert(hit, source="sec_formd")
    if hit is not None:
        return hit
    acct = Account(
        domain=stub_domain(cik10),
        name=fd.entity_name,
        legal_name=fd.entity_name,
        cik=cik10,
        industry=fd.industry_group,
        hq_region=fd.state,
        founded=fd.year_of_inc,
        company_type=fd.entity_type,
        seed_source="sec_formd",
        cohort=cohort or "formd",
    )
    return registry.upsert(acct, source="sec_formd")

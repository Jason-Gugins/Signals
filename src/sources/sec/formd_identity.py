"""Attach a parsed Form D issuer to the account registry."""

from __future__ import annotations

from src.core.models import Account
from src.identity.domains import root_domain
from src.identity.edgar_ids import pad_cik
from src.sources.sec.parse_formd import FormD


def stub_domain(cik: str) -> str:
    return f"cik{pad_cik(cik)}.edgar"


def _is_stub(domain: str) -> bool:
    return domain.startswith("cik") and domain.endswith(".edgar")


def attach_form_d_account(fd: FormD, registry, *, cohort: str | None = "formd", prefer_domain=None) -> Account:
    cik10 = pad_cik(fd.cik)
    if prefer_domain:
        root = root_domain(prefer_domain) or prefer_domain
        hit = registry.get(root)
        cik_hit = registry.resolve(cik=cik10)
        if cik_hit is not None and not _is_stub(cik_hit.domain):
            return cik_hit
        if hit is None and fd.entity_name:
            hit = registry.resolve(name=fd.entity_name)
        if hit is None and cik_hit is not None:
            hit = cik_hit
        if hit is not None:
            if not hit.cik:
                hit.cik = cik10
            old = hit.domain
            if _is_stub(old):
                hit.domain = root
            got = registry.upsert(hit, source="sec_formd")
            if old != got.domain:
                registry.db.execute("DELETE FROM accounts WHERE domain=?", (old,))
                try:
                    registry.db.execute("UPDATE account_aliases SET domain=? WHERE domain=?", (got.domain, old))
                except Exception:
                    pass
            return got
        return registry.upsert(
            Account(
                domain=root,
                name=fd.entity_name,
                legal_name=fd.entity_name,
                cik=cik10,
                industry=fd.industry_group,
                hq_region=fd.state,
                founded=fd.year_of_inc,
                company_type=fd.entity_type,
                seed_source="sec_formd",
                cohort=cohort or "formd",
            ),
            source="sec_formd",
        )
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

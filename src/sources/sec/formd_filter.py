"""PURE Form D lead keep/drop filters."""

from __future__ import annotations

from dataclasses import dataclass

from src.sources.sec.parse_formd import FormD

POOLED = "Pooled Investment Fund"


@dataclass(frozen=True)
class FormDFilter:
    include_funds: bool = False
    include_amendments: bool = False
    min_sold: float = 0.0
    state: str | None = None


def amount_usd(fd: FormD) -> float | None:
    if fd.total_sold not in (None, 0.0):
        return fd.total_sold
    return fd.total_offering


def keep_form_d(fd: FormD, filt: FormDFilter) -> bool:
    if not filt.include_funds and (fd.industry_group or "") == POOLED:
        return False
    if fd.is_amendment and not filt.include_amendments:
        return False
    amt = amount_usd(fd)
    if filt.min_sold and (amt is None or amt < filt.min_sold):
        return False
    if filt.state and (fd.state or "").casefold() != filt.state.casefold():
        return False
    return True

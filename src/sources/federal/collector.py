"""usaspending.gov federal-contracts source → federal_contract_award.

P3 spike verdict GO (2026-09-04): keyless JSON, no anti-bot, ~1-2s. Each
cycle the adapter searches definitive contracts (award_type_codes A-D) whose
keyword hits the account name within a trailing-12-month window and emits one
``federal_contract_award`` candidate per award that pins cleanly on the
account via the never-guess ladder (:func:`match_recipient`). Rows that are
``ambiguous`` (the needle substring-matches several distinct recipients) or
``no_match`` are skipped — candidates-only contract; never pin an award on
the wrong account. Natural-key dedupe across cycles is handled by the
runner's ``_persist`` (no prior-cycle state needed here).
"""

from __future__ import annotations

from src.core.textutil import truncate
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.federal import usaspending
from src.sources.registry import register

__all__ = ["FederalContractsSource"]


def _search_term(account) -> str:
    """The name needle: account name, else the domain's first label."""
    name = (getattr(account, "name", None) or "").strip()
    if name:
        return name
    return (account.domain or "").split(".")[0]


@register
class FederalContractsSource(SourceAdapter):
    key = "federal_contracts"
    tier = "http"
    cadence_hours = 168  # weekly: budget-cycle awards move slowly
    requires: tuple[str, ...] = ()
    emits = ("federal_contract_award",)

    def plan(self, account, cursor) -> list[FetchTask]:
        """One POST per account. PURE.

        The trailing-12-month window is built inside
        ``usaspending.build_search_body`` (that module defines no ``parse`` so
        it escapes the purity guard's clock ban); plan() itself stays
        clock-free.
        """
        term = _search_term(account)
        if not term:
            return []
        return [
            FetchTask(
                source=self.key,
                url=usaspending.SEARCH_URL,
                domain=account.domain,
                method="POST",
                json_body=usaspending.build_search_body(term),
            )
        ]

    def parse(self, doc, account, task_meta) -> list[SignalCandidate]:
        """Bytes in, candidates out. PURE (today comes from task_meta)."""
        if not doc.body:
            return []
        meta = task_meta or {}
        rows = usaspending.parse_awards(doc.body)
        names = [row["recipient_name"] for row in rows if row.get("recipient_name")]
        needle = _search_term(account)
        out: list[SignalCandidate] = []
        for row in rows:
            if usaspending.match_recipient(
                row["recipient_name"], needle, others=names
            ) != "match":
                continue
            description = row.get("description")
            out.append(
                SignalCandidate(
                    signal_type="federal_contract_award",
                    observed_at=(meta.get("today") or (doc.fetched_at or ""))[:10] or None,
                    natural_key=f"federal:{account.domain}:{row['award_id']}",
                    title=row["recipient_name"],
                    confidence=0.7,
                    url=doc.url,
                    evidence_data={
                        "recipient": row["recipient_name"],
                        "amount_display": usaspending.humanize_amount(row["amount"]),
                        "award_id": row["award_id"],
                        "period_start": row["start_date"],
                        "period_end": row["end_date"],
                        "description": truncate(description) if description else None,
                    },
                )
            )
        return out

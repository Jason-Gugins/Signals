from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from src.core.textutil import parse_count, to_iso_date
from src.identity.names import name_similarity, normalize_name
from src.identity.registry import AccountRegistry
from src.sources.base import SignalCandidate


@dataclass(frozen=True)
class WarnNotice:
    company_raw: str
    state: str
    notice_date: str | None
    effective_date: str | None
    affected: int | None
    location: str | None
    url: str | None
    reason: str | None


class WarnJurisdiction(Protocol):
    code: str
    index_url: str
    fmt: str

    def discover(self, body: bytes) -> list[str]: ...
    def parse(self, body: bytes) -> list[WarnNotice]: ...


def parse_affected(raw: str | None) -> int | None:
    if raw is None or str(raw).strip().upper() in {"", "N/A", "NA", "-"}:
        return None
    return parse_count(str(raw))


def warn_to_candidate(notice: WarnNotice, *, today: date) -> SignalCandidate:
    observed = notice.notice_date or notice.effective_date or today.isoformat()
    norm = normalize_name(notice.company_raw) or "unknown"
    return SignalCandidate(
        signal_type="layoff",
        observed_at=observed,
        natural_key=f"warn:{notice.state}:{norm}:{notice.notice_date}:{notice.affected}",
        title=f"WARN {notice.state}: {notice.company_raw}",
        url=notice.url,
        confidence=0.95,
        evidence_data={
            "affected": notice.affected,
            "location": notice.location,
            "effective_date": notice.effective_date,
            "state": notice.state,
        },
    )


def match_notices(
    notices: list[WarnNotice],
    registry: AccountRegistry,
    *,
    min_similarity: float = 0.92,
    unmatched_path: str | None = None,
) -> list[tuple[WarnNotice, str]]:
    accounts = registry.list_accounts(disqualified=False, limit=None) + registry.list_accounts(disqualified=True, limit=None)
    # list_accounts filters disqualified; get all via query
    rows = registry.db.query("SELECT * FROM accounts")
    from src.core.models import Account
    accounts = [Account.from_db_row(r) for r in rows]
    matched = []
    unmatched = []
    for n in notices:
        hits = []
        for a in accounts:
            if not a.name:
                continue
            if name_similarity(n.company_raw, a.name) >= min_similarity:
                hits.append(a.domain)
        if len(hits) == 1:
            matched.append((n, hits[0]))
        else:
            unmatched.append(n)
    if unmatched_path:
        path = Path(unmatched_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["company_raw,state,notice_date,affected"]
        for n in unmatched:
            lines.append(f"\"{n.company_raw}\",{n.state},{n.notice_date or ''},{n.affected or ''}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return matched

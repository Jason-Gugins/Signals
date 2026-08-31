"""Pure tech-stack change detection: diff previous vs current vendor sets."""

from __future__ import annotations

from src.sources.base import SignalCandidate


def diff_technologies(
    previous: set[str],
    current: set[str],
    *,
    domain: str,
    today: str,
    flap_guard: set[str] | None = None,
) -> list[tuple[str, SignalCandidate]]:
    """Diff the prior cycle's vendor set against the current one.

    Returns (signal_type, SignalCandidate) pairs, sorted by vendor name:
      - added (current - previous)   -> tech_install_new, confidence 0.7
      - removed (previous - current) -> tech_churn, confidence 0.6

    Vendors listed in ``flap_guard`` (those seen churning in the immediately
    prior diff) are suppressed from churn emission. Pure: no I/O.
    """
    guard = flap_guard or set()
    out: list[tuple[str, SignalCandidate]] = []
    for vendor in sorted((current - previous) | (previous - current)):
        if vendor in current:
            out.append((
                "tech_install_new",
                SignalCandidate(
                    "tech_install_new",
                    today,
                    f"techchg:{domain}:{vendor}:{today}",
                    title=vendor,
                    confidence=0.7,
                    evidence_data={"vendor": vendor, "change": "install"},
                ),
            ))
        elif vendor not in guard:
            out.append((
                "tech_churn",
                SignalCandidate(
                    "tech_churn",
                    today,
                    f"techchg:{domain}:{vendor}:{today}",
                    title=vendor,
                    confidence=0.6,
                    evidence_data={"vendor": vendor, "change": "churn"},
                ),
            ))
    return out

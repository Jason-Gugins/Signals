"""Feb-29-safe one-year-ago helper and the GitHub stagnation guard (Task 13)."""

from datetime import date

from src.core.models import Account
from src.sources.community.github import _one_year_ago, github_to_candidates

ACCT = Account(domain="acme.com", name="Acme")


def _repo(pushed_at: str) -> dict:
    return {"id": 1, "full_name": "acme/repo", "pushed_at": pushed_at}


def _stagnation(repos: list[dict], today: date):
    return [
        c
        for c in github_to_candidates(None, repos, [], ACCT, today=today)
        if c.signal_type == "stagnation"
    ]


def test_one_year_ago_leap_day_clamps_to_feb_28():
    assert _one_year_ago(date(2024, 2, 29)) == date(2023, 2, 28)


def test_one_year_ago_plain_dates_unchanged():
    assert _one_year_ago(date(2026, 9, 4)) == date(2025, 9, 4)
    assert _one_year_ago(date(2025, 12, 31)) == date(2024, 12, 31)


def test_feb_29_verdict_matches_non_leap_logic():
    # Non-leap reference: today=2023-03-01, one year before is 2022-03-01.
    ref = date(2023, 3, 1)
    assert _stagnation([_repo("2022-03-01")], ref) == []  # pushed == one_year_ago -> fresh
    assert _stagnation([_repo("2022-02-28")], ref) != []  # one day older -> stale
    # Leap mirror: today=2024-02-29, one_year_ago clamps to 2023-02-28.
    leap = date(2024, 2, 29)
    assert _stagnation([_repo("2023-02-28")], leap) == []  # pushed == one_year_ago -> fresh
    assert _stagnation([_repo("2023-02-27")], leap) != []  # one day older -> stale
    assert _stagnation([_repo("2023-03-01")], leap) == []  # just under one year -> fresh


def test_stagnation_fires_when_repos_report_no_push_dates():
    assert _stagnation([{"id": 1}], date(2024, 2, 29)) != []
    assert _stagnation([{"id": 1}], date(2026, 9, 4)) != []


def test_no_stagnation_without_repos():
    assert _stagnation([], date(2024, 2, 29)) == []

import re

from src.sources.jobsignals.analyze import _required_stack, extract_required_stack_demands


VOCAB = [
    ("snowflake", re.compile(r"\bsnowflake\b", re.I)),
    ("dbt", re.compile(r"\bdbt\b", re.I)),
]


def test_required_stack_demands_include_exact_phrase():
    desc = "Required: hands-on experience with Snowflake and dbt."
    rows = extract_required_stack_demands(desc, VOCAB)
    assert {r["vendor"] for r in rows} == {"snowflake", "dbt"}
    assert all(r["phrase"] for r in rows)
    for row in rows:
        assert row["phrase"] in desc


def test_optional_stack_mention_is_not_required_demand():
    assert extract_required_stack_demands("Snowflake is a plus.", VOCAB) == []


def test_proficiency_phrasing_is_a_demand():
    rows = extract_required_stack_demands("Proficiency with dbt and Snowflake is required.", VOCAB)
    assert {r["vendor"] for r in rows} == {"snowflake", "dbt"}


def test_unknown_vendor_is_ignored():
    assert extract_required_stack_demands("Experience with Zorblex is required.", VOCAB) == []


def test_required_stack_wrapper_is_unchanged():
    desc = "Required: hands-on experience with Snowflake and dbt."
    assert _required_stack(desc, VOCAB) == ["snowflake", "dbt"]
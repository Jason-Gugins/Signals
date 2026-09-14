import pytest
from pathlib import Path

from src.core.config import ConfigError
from src.intel.market import load_market_profile, match_relevance


PROFILE_YAML = '''profiles:
  p:
    seller: TestCo
    offerings:
      - id: data-platform
        buyer_departments: [engineering, data]
        served_problem_phrases: [consolidate data, data quality]
        required_vendors: [snowflake, dbt]
        competitor_vendors: [databricks]
        relevant_signal_types: [need_statement, required_stack_demand]
'''


def _profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "markets.yaml"
    path.write_text(PROFILE_YAML, encoding="utf-8")
    return path


def test_default_profile_loads_and_is_inert_until_filled():
    profile = load_market_profile("default", Path("config/markets.yaml"))
    assert profile.profile_id == "default"
    assert profile.is_empty is True
    assert match_relevance(
        text="We are consolidating data systems.",
        department="data",
        signal_type="need_statement",
        vendors=[],
        profile=profile,
    ) is None


def test_explicit_need_matches_phrase_and_department(tmp_path):
    profile = load_market_profile("p", _profile_file(tmp_path))
    assert profile.is_empty is False
    hit = match_relevance(
        text="We are consolidating data to improve data quality.",
        department="Data",
        signal_type="need_statement",
        vendors=[],
        profile=profile,
    )
    assert hit is not None
    assert hit.offering_id == "data-platform"
    assert "phrase:data quality" in hit.reasons
    assert "department:Data" in hit.reasons


def test_required_vendor_match_needs_no_phrase(tmp_path):
    profile = load_market_profile("p", _profile_file(tmp_path))
    hit = match_relevance(
        text="Required: hands-on Snowflake experience.",
        department="engineering",
        signal_type="required_stack_demand",
        vendors=["Snowflake"],
        profile=profile,
    )
    assert hit is not None
    assert "vendor:snowflake" in hit.reasons


def test_unrelated_need_is_not_promoted(tmp_path):
    profile = load_market_profile("p", _profile_file(tmp_path))
    assert match_relevance(
        text="We are redesigning the office kitchen.",
        department="facilities",
        signal_type="need_statement",
        vendors=[],
        profile=profile,
    ) is None
    assert match_relevance(
        text="We are consolidating data to improve data quality.",
        department="facilities",
        signal_type="need_statement",
        vendors=[],
        profile=profile,
    ) is None


def test_unknown_profile_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown market profile"):
        load_market_profile("missing", _profile_file(tmp_path))

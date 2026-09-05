"""BBB profile source package (spike verdict: GO plain-fetch — see data/probe/P2_SOURCE_SPIKE.md §5)."""

from src.sources.bbb.collector import (
    BBB_GRADE_SCALE,
    BbbProfileSource,
    grade_ordinal,
    parse_bbb_profile,
)

__all__ = ["BbbProfileSource", "parse_bbb_profile", "grade_ordinal", "BBB_GRADE_SCALE"]

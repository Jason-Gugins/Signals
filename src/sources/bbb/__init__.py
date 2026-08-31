"""BBB profile source package (spike verdict: GO plain-fetch — see data/probe/P2_SOURCE_SPIKE.md §5)."""

from src.sources.bbb.collector import BbbProfileSource, parse_bbb_profile

__all__ = ["BbbProfileSource", "parse_bbb_profile"]

"""Task 6 (P3): raw-store quota math — stats + over-quota detection."""

from __future__ import annotations

from src.core.rawstore import RawStore
from src.pipeline.health import raw_quota_check

MB = 1024 * 1024


def _store(tmp_path) -> RawStore:
    from src.core.db import Database

    return RawStore(Database(tmp_path / "s.db"), raw_dir=tmp_path / "raw")


def test_stats_byte_totals(tmp_path):
    store = _store(tmp_path)
    store.put(source="a", url="https://e/1", body=b"x" * (3 * MB), content_type="text/plain", status=200)
    store.put(source="b", url="https://e/2", body=b"y" * (2 * MB), content_type="text/plain", status=200)
    s = store.stats()
    assert s["docs"] == 2
    assert s["bytes"] == 5 * MB


def test_raw_quota_check_disabled_returns_none():
    assert raw_quota_check(999.0, None) is None
    assert raw_quota_check(999.0, 0) is None


def test_raw_quota_check_under_and_boundary_is_ok():
    under = raw_quota_check(1.0, 2.0)
    assert under == {"status": "OK", "used_mb": 1.0, "quota_mb": 2.0}
    at = raw_quota_check(2.0, 2.0)
    assert at["status"] == "OK"  # exactly at quota is not exceeded


def test_raw_quota_check_over_is_warn():
    over = raw_quota_check(3.0, 2.0)
    assert over == {"status": "WARN", "used_mb": 3.0, "quota_mb": 2.0}

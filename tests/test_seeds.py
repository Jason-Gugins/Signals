"""Tests for CSV / LinkedIn / RepVue account seeders."""

from __future__ import annotations

import time
from pathlib import Path

from src.core.db import Database
from src.identity.registry import AccountRegistry
from src.identity.seeds import seed_from_csv, seed_from_linkedin_db, seed_from_repvue_db
from tests.fixtures.db.make_fake_dbs import build_linkedin, build_repvue


def _reg(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "signals.db"))


def test_csv_all_columns_and_minimal(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(
        "domain,website,name,industry,employee_count,hq_country,ticker,cik,linkedin_slug,careers_url\n"
        "gong.io,,Gong,Software,1200,United States,GONG,000123,gong-io,https://gong.io/careers\n"
        ",https://www.shopify.com,Shopify,,,,,,,\n",
        encoding="utf-8",
    )
    stats = seed_from_csv(_reg(tmp_path), csv_path, cohort="ca")
    assert stats.created == 2
    assert stats.skipped == 0
    reg = _reg(tmp_path)
    # fresh registry on same db? we used a new Database path above. reuse:
    db = Database(tmp_path / "signals.db")
    reg = AccountRegistry(db)
    gong = reg.get("gong.io")
    assert gong.name == "Gong"
    assert gong.industry == "Software"
    assert gong.employee_count == 1200
    assert gong.hq_country == "United States"
    assert gong.ticker == "GONG"
    assert gong.cohort == "ca"
    assert reg.get("shopify.com").name == "Shopify"


def test_csv_name_only_skipped_no_domain(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text("name\nNameless Corp\n", encoding="utf-8")
    stats = seed_from_csv(_reg(tmp_path), csv_path)
    assert stats.created == 0
    assert stats.skipped == 1
    assert stats.reasons["no_domain"] == 1


def test_csv_duplicates_collapse(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text(
        "domain,name,industry\n"
        "acme.com,Acme,\n"
        "https://www.acme.com,Acme Inc,Software\n",
        encoding="utf-8",
    )
    stats = seed_from_csv(_reg(tmp_path), csv_path)
    assert stats.created == 1
    assert stats.updated == 1
    db = Database(tmp_path / "signals.db")
    assert db.one("SELECT COUNT(*) AS n FROM accounts")["n"] == 1
    assert AccountRegistry(db).get("acme.com").industry == "Software"


def test_linkedin_maps_fields_and_is_readonly(tmp_path):
    li = build_linkedin(tmp_path / "linkedin.db")
    mtime_before = li.stat().st_mtime
    time.sleep(0.05)
    reg = _reg(tmp_path)
    stats = seed_from_linkedin_db(reg, str(li), cohort="override")
    assert stats.created >= 2
    gong = reg.get("gong.io")
    assert gong.name == "Gong"
    assert gong.linkedin_slug == "gong-io"
    assert gong.linkedin_company_id == "12345"
    assert gong.industry == "Computer Software"
    assert gong.employee_count == 1200
    assert gong.founded == "2015"
    assert gong.company_type == "Public Company"
    assert gong.hq_city == "San Francisco"
    assert gong.hq_region == "California"
    assert gong.hq_country == "United States"
    assert gong.extra_data["headquarters_raw"] == "San Francisco, California, United States"
    assert gong.cohort == "override"
    shop = reg.get("shopify.com")
    assert shop.hq_country == "Canada"
    mtime_after = li.stat().st_mtime
    assert mtime_after == mtime_before
    from src.identity.seeds import _open_ro

    conn = _open_ro(str(li))
    assert conn.execute("PRAGMA query_only").fetchone()[0] in (1, "1")
    conn.close()


def test_repvue_nested_extra_data(tmp_path):
    rv = build_repvue(tmp_path / "repvue.db")
    reg = _reg(tmp_path)
    stats = seed_from_repvue_db(reg, str(rv))
    assert stats.created >= 2
    gong = reg.get("gong.io")
    assert gong.repvue_slug == "gong"
    assert gong.extra_data["repvue"]["repvue_score"] == 4.2
    assert gong.extra_data["repvue"]["ratings_count"] == 88
    assert gong.extra_data["repvue"]["quota_attainment"] == 0.61


def test_rerun_reports_updated(tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text("domain,name\nacme.com,Acme\n", encoding="utf-8")
    reg = _reg(tmp_path)
    first = seed_from_csv(reg, csv_path)
    second = seed_from_csv(reg, csv_path)
    assert first.created == 1
    assert second.created == 0
    assert second.updated == 1
    li = build_linkedin(tmp_path / "li.db")
    a = seed_from_linkedin_db(reg, str(li))
    b = seed_from_linkedin_db(reg, str(li))
    assert b.created == 0
    assert b.updated >= 1
    rv = build_repvue(tmp_path / "rv.db")
    seed_from_repvue_db(reg, str(rv))
    again = seed_from_repvue_db(reg, str(rv))
    assert again.created == 0
    assert again.updated >= 1

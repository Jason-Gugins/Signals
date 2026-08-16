"""Tests for domain lists and champion CSV loading."""

from __future__ import annotations

from src.core.db import Database
from src.identity.lists import load_champions, load_domain_list, upsert_champions


def test_load_domain_list_ignores_comments_and_normalizes(tmp_path):
    p = tmp_path / "dnc.txt"
    p.write_text(
        "# competitors\n"
        "Gong.IO\n"
        "\n"
        "https://WWW.Acme.com/about\n"
        "# trailing\n",
        encoding="utf-8",
    )
    got = load_domain_list(str(p))
    assert got == {"gong.io", "acme.com"}


def test_champion_rows_need_identifier(tmp_path):
    p = tmp_path / "champions.csv"
    p.write_text(
        "name,linkedin_slug,prior_company,prior_domain,relationship,last_touch,notes\n"
        "Jane Doe,jane-doe,Gong,gong.io,former AE,2026-01,good\n"
        "No Id,,,,friend,,\n"
        "Sam OnlyName,,,,,,\n"
        "Pat Prior,,Acme,acme.com,buyer,2025-12,\n",
        encoding="utf-8",
    )
    rows = load_champions(str(p))
    assert len(rows) == 2
    assert rows[0]["name"] == "Jane Doe"
    assert rows[0]["linkedin_slug"] == "jane-doe"
    assert rows[1]["prior_domain"] == "acme.com"


def test_upsert_champions_sets_flag(tmp_path):
    db = Database(tmp_path / "signals.db")
    n = upsert_champions(
        db,
        [
            {
                "name": "Jane Doe",
                "linkedin_slug": "jane-doe",
                "prior_domain": "gong.io",
                "prior_company": "Gong",
                "relationship": "former AE",
                "last_touch": "2026-01",
                "notes": "good",
            }
        ],
    )
    assert n == 1
    row = db.one("SELECT * FROM contacts WHERE person_key = ?", ("jane-doe",))
    assert row["is_champion"] == 1
    assert row["name"] == "Jane Doe"
    assert row["prior_domain"] == "gong.io"

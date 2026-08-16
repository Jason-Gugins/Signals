"""Build tiny read-only-shaped LinkedIn and RepVue SQLite fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path


LI_SCHEMA = """
CREATE TABLE companies (
    name TEXT PRIMARY KEY,
    linkedin_url TEXT,
    linkedin_slug TEXT,
    website TEXT,
    domain TEXT,
    industry TEXT,
    company_size TEXT,
    employee_count INTEGER,
    headquarters TEXT,
    founded TEXT,
    company_type TEXT,
    specialties TEXT,
    about TEXT,
    follower_count INTEGER,
    seed_source TEXT,
    cohort TEXT,
    extra_data TEXT,
    scraped_at TEXT,
    profile_scraped INTEGER DEFAULT 0,
    profile_scraped_at TEXT,
    linkedin_company_id TEXT
);
"""

RV_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    slug TEXT,
    url TEXT,
    industry TEXT,
    repvue_score REAL,
    ratings_count INTEGER,
    hiring INTEGER DEFAULT 0,
    description TEXT,
    quota_attainment_display TEXT,
    quota_attainment REAL,
    company_size TEXT,
    headquarters TEXT,
    funding_stage TEXT,
    website TEXT,
    domain TEXT,
    employee_headcount INTEGER,
    founded TEXT
);
"""


def build_linkedin(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(LI_SCHEMA)
    conn.executemany(
        """INSERT INTO companies(
            name, linkedin_url, linkedin_slug, website, domain, industry,
            employee_count, headquarters, founded, company_type,
            linkedin_company_id, cohort
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "Gong",
                "https://www.linkedin.com/company/gong-io",
                "gong-io",
                "https://www.gong.io",
                "gong.io",
                "Computer Software",
                1200,
                "San Francisco, California, United States",
                "2015",
                "Public Company",
                "12345",
                "ca-software",
            ),
            (
                "Shopify",
                "https://www.linkedin.com/company/shopify",
                "shopify",
                "https://www.shopify.com",
                "shopify.com",
                "Internet",
                11000,
                "Ottawa, Ontario, Canada",
                "2006",
                "Public Company",
                "67890",
                "ca-software",
            ),
            (
                "No Website Inc",
                "https://www.linkedin.com/company/noweb",
                "noweb",
                None,
                None,
                "Staffing",
                20,
                "Remote",
                None,
                "Privately Held",
                None,
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return path


def build_repvue(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(RV_SCHEMA)
    conn.executemany(
        """INSERT INTO companies(
            name, slug, url, industry, website, domain, company_size,
            headquarters, funding_stage, repvue_score, ratings_count, quota_attainment
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "Gong",
                "gong",
                "https://www.repvue.com/companies/gong",
                "Software",
                "https://www.gong.io",
                "gong.io",
                "1001-5000",
                "San Francisco, CA",
                "Series E",
                4.2,
                88,
                0.61,
            ),
            (
                "HubSpot",
                "hubspot",
                "https://www.repvue.com/companies/hubspot",
                "Software",
                "https://www.hubspot.com",
                "hubspot.com",
                "5001-10000",
                "Cambridge, MA",
                "Public",
                4.0,
                200,
                0.55,
            ),
            (
                "Orphan Co",
                "orphan-co",
                "https://www.repvue.com/companies/orphan-co",
                "Other",
                None,
                None,
                "1-50",
                None,
                None,
                None,
                None,
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    root = Path(__file__).parent
    build_linkedin(root / "linkedin.db")
    build_repvue(root / "repvue.db")
    print("wrote", root / "linkedin.db", root / "repvue.db")

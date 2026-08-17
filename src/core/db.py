"""SQLite database layer with schema, additive migrations, and coalescing upsert."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


SCHEMA_SQL = """
-- ── Accounts: the identity spine. domain is the canonical key. ─────────────
CREATE TABLE IF NOT EXISTS accounts (
    domain              TEXT PRIMARY KEY,          -- root domain, lowercase, no www
    name                TEXT,
    legal_name          TEXT,
    linkedin_slug       TEXT,
    linkedin_company_id TEXT,
    repvue_slug         TEXT,
    cik                 TEXT,                      -- 10-digit zero-padded
    ticker              TEXT,
    ats_vendor          TEXT,                      -- greenhouse|lever|ashby|...
    ats_token           TEXT,
    careers_url         TEXT,
    blog_feed_url       TEXT,
    industry            TEXT,
    sic_code            TEXT,
    employee_count      INTEGER,
    employee_count_at   TEXT,
    hq_country          TEXT,
    hq_region           TEXT,
    hq_city             TEXT,
    founded             TEXT,
    company_type        TEXT,                      -- public|private|nonprofit|...
    cohort              TEXT,                      -- free-form campaign tag
    seed_source         TEXT,
    icp_fit             REAL,                      -- 0..2 multiplier, 1.0 = neutral
    icp_reasons         TEXT,                      -- JSON list
    disqualified        INTEGER DEFAULT 0,
    disqualify_reason   TEXT,
    score               REAL,
    tier                INTEGER,
    buying_window       TEXT,                      -- active|opening|developing|dormant
    scored_at           TEXT,
    extra_data          TEXT,                      -- JSON
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_accounts_tier    ON accounts(tier);
CREATE INDEX IF NOT EXISTS idx_accounts_score   ON accounts(score DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_cohort  ON accounts(cohort);
CREATE INDEX IF NOT EXISTS idx_accounts_li      ON accounts(linkedin_slug);
CREATE INDEX IF NOT EXISTS idx_accounts_cik     ON accounts(cik);

-- ── Aliases: every other string that means "this account". ────────────────
CREATE TABLE IF NOT EXISTS account_aliases (
    alias        TEXT NOT NULL,        -- normalized (lowercased, punctuation-stripped)
    alias_kind   TEXT NOT NULL,        -- name|domain|linkedin_slug|ticker|cik|ats_token|subdomain
    domain       TEXT NOT NULL,
    confidence   REAL DEFAULT 1.0,
    source       TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (alias, alias_kind)
);
CREATE INDEX IF NOT EXISTS idx_alias_domain ON account_aliases(domain);

-- ── Contacts: humans. person_key = linkedin slug when known else hash. ────
CREATE TABLE IF NOT EXISTS contacts (
    person_key      TEXT PRIMARY KEY,
    domain          TEXT,
    name            TEXT,
    title           TEXT,
    seniority       TEXT,             -- c_level|vp|director|head|manager|ic|unknown
    persona         TEXT,             -- economic_buyer|champion|technical|user|unknown
    department      TEXT,
    linkedin_slug   TEXT,
    linkedin_url    TEXT,
    location        TEXT,
    is_champion     INTEGER DEFAULT 0,  -- known past user/buyer (from champions.csv)
    role_started_at TEXT,
    prior_domain    TEXT,               -- for champion_migration
    email_guess     TEXT,
    email_pattern   TEXT,
    extra_data      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);
CREATE INDEX IF NOT EXISTS idx_contacts_champ  ON contacts(is_champion);

-- ── Signals: the product of the whole system. ─────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    signal_id     TEXT PRIMARY KEY,   -- sha256(domain|type|natural_key)[:16]
    domain        TEXT NOT NULL,
    person_key    TEXT,
    signal_type   TEXT NOT NULL,
    category      TEXT NOT NULL,      -- financial|hiring|technology|operational|negative|neutral|intent
    degree        INTEGER DEFAULT 0,  -- 0=trigger, 1|2|3 = intent degree
    origin        TEXT NOT NULL,      -- internal|external
    catalyst      TEXT NOT NULL,      -- primary|secondary
    polarity      TEXT NOT NULL,      -- positive|negative|neutral
    title         TEXT,
    summary       TEXT,
    evidence      TEXT,               -- rendered one-liner for the rep
    evidence_data TEXT,               -- JSON: template vars (round_stage, competitor, ...)
    url           TEXT,
    source        TEXT NOT NULL,      -- source adapter key
    confidence    REAL DEFAULT 0.8,   -- 0..1
    observed_at   TEXT NOT NULL,      -- ISO date the EVENT happened (not fetch time)
    first_seen_at TEXT NOT NULL,      -- when we first recorded it
    last_seen_at  TEXT NOT NULL,
    raw_ref       TEXT,               -- documents.doc_id
    superseded_by TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_domain   ON signals(domain);
CREATE INDEX IF NOT EXISTS idx_signals_type     ON signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_signals_observed ON signals(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_source   ON signals(source);

-- ── Score history (append-only; lets you chart momentum). ─────────────────
CREATE TABLE IF NOT EXISTS score_history (
    domain      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    score       REAL,
    tier        INTEGER,
    buying_window TEXT,
    components  TEXT,        -- JSON: per-signal contributions + combos fired
    PRIMARY KEY (domain, as_of)
);

-- ── Play assignments. ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS play_assignments (
    domain      TEXT NOT NULL,
    play_id     TEXT NOT NULL,
    signal_id   TEXT,
    rank        INTEGER,
    urgency     INTEGER,
    variables   TEXT,        -- JSON
    opener      TEXT,        -- rendered
    t24         TEXT,        -- rendered
    generated_at TEXT,
    PRIMARY KEY (domain, play_id, signal_id)
);

-- ── Raw document store index. ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,     -- sha256 of body bytes
    source        TEXT NOT NULL,
    domain        TEXT,
    url           TEXT,
    method        TEXT DEFAULT 'GET',
    content_type  TEXT,
    status        INTEGER,
    byte_size     INTEGER,
    path          TEXT,                 -- data/raw/<xx>/<sha>.gz
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT,
    parsed_at     TEXT,
    parse_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);

-- ── Cursors for incremental polling. ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_cursors (
    source        TEXT NOT NULL,
    key           TEXT NOT NULL,   -- usually the domain, or 'global'
    cursor        TEXT,            -- source-defined (date, accession no, page token)
    etag          TEXT,
    last_modified TEXT,
    last_run_at   TEXT,
    next_due_at   TEXT,
    fail_count    INTEGER DEFAULT 0,
    last_error    TEXT,
    PRIMARY KEY (source, key)
);
CREATE INDEX IF NOT EXISTS idx_cursors_due ON source_cursors(next_due_at);

-- ── Per-request audit. ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    source     TEXT,
    domain     TEXT,
    url        TEXT,
    status     INTEGER,
    elapsed_ms INTEGER,
    bytes      INTEGER,
    cached     INTEGER DEFAULT 0,
    error      TEXT,
    at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fetchlog_run ON fetch_log(run_id);

-- ── Run audit. ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    stage       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    accounts    INTEGER DEFAULT 0,
    documents   INTEGER DEFAULT 0,
    signals_new INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running',
    notes       TEXT
);

-- ── Watchlist for closed-lost / dormant re-engagement. ────────────────────
CREATE TABLE IF NOT EXISTS watchlist (
    domain         TEXT PRIMARY KEY,
    reason         TEXT,            -- closed_lost|t24_failed|dormant|manual
    notes          TEXT,
    added_at       TEXT,
    last_alert_at  TEXT,
    alert_on_types TEXT             -- JSON list; null = any primary trigger
);

CREATE TABLE IF NOT EXISTS jobs (
    job_key       TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,
    source        TEXT NOT NULL,
    external_id   TEXT,
    title         TEXT,
    department    TEXT,
    team          TEXT,
    location_raw  TEXT,
    city TEXT, region TEXT, country TEXT, remote INTEGER,
    employment_type TEXT, seniority TEXT,
    description   TEXT,
    url           TEXT,
    posted_at     TEXT,
    first_seen_at TEXT, last_seen_at TEXT, closed_at TEXT,
    comp_min REAL, comp_max REAL, comp_currency TEXT,
    extra_data    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_domain ON jobs(domain);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_dept   ON jobs(domain, department);

CREATE TABLE IF NOT EXISTS job_snapshots (
    domain TEXT NOT NULL, as_of TEXT NOT NULL,
    open_count INTEGER, by_department TEXT, by_country TEXT,
    PRIMARY KEY (domain, as_of)
);

CREATE TABLE IF NOT EXISTS technologies (
    domain TEXT NOT NULL, vendor TEXT NOT NULL, category TEXT, tier TEXT,
    first_seen_at TEXT, last_seen_at TEXT, missing_runs INTEGER DEFAULT 0,
    evidence TEXT, confidence REAL, source TEXT,
    PRIMARY KEY (domain, vendor)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    domain TEXT NOT NULL, as_of TEXT NOT NULL, employee_count INTEGER,
    repvue_score REAL, open_jobs INTEGER, followers INTEGER,
    PRIMARY KEY (domain, as_of)
);
"""

# table -> {column: type-with-default}  — populated by later tasks
NEW_COLUMNS: dict[str, dict[str, str]] = {
    "accounts": {},
    "signals": {},
    "documents": {},
}


class Database:
    def __init__(self, db_path: str | Path = "data/signals.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._connect()
        self._migrate()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is closed")
        return self._conn

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        for table, cols in NEW_COLUMNS.items():
            existing = self.table_columns(table)
            for col, col_type in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        self.conn.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def upsert(
        self,
        table: str,
        row: dict,
        pk: str | tuple[str, ...],
        coalesce: bool = True,
        overwrite: set[str] | None = None,
    ) -> None:
        overwrite = overwrite or set()
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(cols)
        pk_cols = (pk,) if isinstance(pk, str) else tuple(pk)
        conflict = ", ".join(pk_cols)
        assignments: list[str] = []
        for col in cols:
            if col in pk_cols:
                continue
            if col in overwrite or not coalesce:
                assignments.append(f"{col} = excluded.{col}")
            else:
                assignments.append(f"{col} = COALESCE(excluded.{col}, {table}.{col})")
        if assignments:
            update_sql = "UPDATE SET " + ", ".join(assignments)
        else:
            update_sql = "NOTHING"
        if update_sql == "NOTHING":
            sql = (
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO NOTHING"
            )
        else:
            sql = (
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO {update_sql}"
            )
        self.execute(sql, [row[c] for c in cols])

    def table_columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

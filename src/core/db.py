"""SQLite database layer with schema, additive migrations, and coalescing upsert."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from loguru import logger


SCHEMA_SQL = """
-- ── Accounts: the identity spine. domain is the canonical key. ─────────────
CREATE TABLE IF NOT EXISTS accounts (
    domain              TEXT PRIMARY KEY,          -- root domain, lowercase, no www
    name                TEXT,
    legal_name          TEXT,
    linkedin_slug       TEXT,
    linkedin_company_id TEXT,
    repvue_slug         TEXT,
    g2_slug             TEXT,
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

CREATE TABLE IF NOT EXISTS cloudflare_cookies (
    domain TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    proxy TEXT NOT NULL,            -- "direct" or the proxy URL — IP binding
    cookies TEXT NOT NULL,          -- JSON array of {name, value, domain, ...} full jar
    expires_at TEXT NOT NULL,       -- tz-aware ISO datetime (real cookie expiry, not a guess)
    solved_at TEXT NOT NULL,        -- when we solved the challenge
    solve_method TEXT,              -- 'browser' | 'solver' | 'headed'
    PRIMARY KEY (domain, user_agent, proxy)
);

CREATE TABLE IF NOT EXISTS datadome_cookies (
    domain          TEXT NOT NULL,
    user_agent      TEXT NOT NULL,
    proxy           TEXT NOT NULL DEFAULT 'direct',
    cookies         TEXT NOT NULL,           -- JSON array of {name, value, domain, ...} full jar
    expires_at      TEXT NOT NULL,           -- tz-aware ISO datetime (real cookie expiry)
    solve_method    TEXT,                    -- 'browser' | 'solver' | '2captcha' | 'capsolver' | 'headed'
    PRIMARY KEY (domain, user_agent, proxy)
);

CREATE TABLE IF NOT EXISTS g2_reviews (
    review_id            TEXT PRIMARY KEY,
    product_slug         TEXT NOT NULL,
    reviewer_name        TEXT,
    reviewer_title       TEXT,
    reviewer_company_size TEXT,
    rating               REAL,
    review_title         TEXT,
    review_body          TEXT,
    pros                 TEXT,
    cons                 TEXT,
    posted_at            TEXT,
    review_url           TEXT,
    verified_reviewer    INTEGER DEFAULT 0,
    review_source        TEXT,
    nps_score            INTEGER,
    helpful_votes        INTEGER,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    raw_ref              TEXT
);
CREATE INDEX IF NOT EXISTS idx_g2_reviews_slug ON g2_reviews(product_slug);
CREATE INDEX IF NOT EXISTS idx_g2_reviews_posted ON g2_reviews(posted_at DESC);
"""

# table -> {column: type-with-default}  — populated by later tasks
NEW_COLUMNS: dict[str, dict[str, str]] = {
    "accounts": {"g2_slug": "TEXT"},
    "signals": {},
    "documents": {},
    "g2_reviews": {
        "nps_score": "INTEGER",
        "helpful_votes": "INTEGER",
        "source": "TEXT DEFAULT 'g2'",
    },
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Apply every NEW_COLUMNS entry idempotently (additive ALTER TABLE)."""
    for table, cols in NEW_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_type in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


# Ordered migrations: (version, description, callable(conn)).
# Version N migrates a DB at user_version == N-1 up to N. The initial
# additive NEW_COLUMNS pass is folded in here (v1) so pre-existing DBs
# (which were at user_version 0 with all columns already applied) upgrade
# cleanly and idempotently.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "additive NEW_COLUMNS pass (g2_slug, nps_score, helpful_votes, source)", _add_missing_columns),
]

LATEST_VERSION: int = max(v for v, _, _ in MIGRATIONS)


class Database:
    def __init__(self, db_path: str | Path = "data/signals.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._connect()
        self._migrate()

    def _connect(self) -> None:
        # check_same_thread=False: HttpFetcher logs from the worker pool.
        # All public methods serialize on self._lock.
        try:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError as exc:
            raise self._corrupt_error(exc) from exc
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _corrupt_error(self, exc: Exception) -> RuntimeError:
        """Build a clear RuntimeError for unreadable/corrupt DB files."""
        detail = str(exc)
        try:
            rows = self._conn.execute("PRAGMA integrity_check").fetchall()
            detail = "; ".join(str(r[0]) for r in rows)
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None
        return RuntimeError(
            f"database file is corrupt or not a SQLite database: "
            f"{self.db_path} ({detail})"
        )

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is closed")
        return self._conn

    def _migrate(self) -> None:
        try:
            ic = self.conn.execute("PRAGMA integrity_check").fetchall()
            status = "; ".join(str(row[0]) for row in ic)
            if status == "ok":
                logger.info("DB integrity_check ok: {}", self.db_path)
            else:
                logger.error("DB integrity_check FAILED for {}: {}", self.db_path, status)
                raise RuntimeError(f"database integrity check failed: {self.db_path} ({status})")

            self.conn.executescript(SCHEMA_SQL)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for ver, desc, fn in MIGRATIONS:
                if ver <= version:
                    continue
                logger.info("Applying DB migration v{}: {}", ver, desc)
                # BEGIN/COMMIT: apply the step atomically alongside the version bump.
                self.conn.execute("BEGIN")
                try:
                    fn(self.conn)
                    self.conn.execute(f"PRAGMA user_version = {ver}")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                self.conn.execute("COMMIT")
            # Late-bound NEW_COLUMNS pass: stays idempotent and honors runtime
            # monkeypatching of NEW_COLUMNS (existing tests depend on that hook).
            _add_missing_columns(self.conn)
            self.conn.commit()
        except sqlite3.DatabaseError as exc:
            raise self._corrupt_error(exc) from exc

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            cur = self.conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
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
        with self._lock:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {row[1] for row in rows}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class CfCookieStore:
    """Persist and reuse the full Cloudflare cookie set (cf_clearance + __cf_bm + cf_chl_*)
    per domain, bound to the UA and egress proxy that solved it.

    Primary key is (domain, user_agent, proxy) so the same domain can have distinct
    jars per browser fingerprint / proxy chain.
    """

    def __init__(self, db: "Database"):
        self.db = db

    def get(self, domain: str, *, user_agent: str, proxy: str = "direct") -> dict | None:
        row = self.db.one(
            "SELECT * FROM cloudflare_cookies WHERE domain=? AND user_agent=? AND proxy=?",
            (domain, user_agent, proxy),
        )
        if row is None:
            return None
        # expiry check via datetime parsing (not ISO string compare — fragile across tz formats)
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        return dict(row)

    def put(
        self,
        domain: str,
        *,
        user_agent: str,
        proxy: str = "direct",
        cookies: list[dict],
        expires_at: str,
        solve_method: str = "browser",
    ) -> None:
        # coalesce=False: a fresh solve MUST overwrite a stale row
        # (db.upsert defaults coalesce=True which would keep the old cookie)
        self.db.upsert(
            "cloudflare_cookies",
            {
                "domain": domain,
                "user_agent": user_agent,
                "proxy": proxy,
                "cookies": json.dumps(cookies),
                "expires_at": expires_at,
                "solved_at": datetime.now(timezone.utc).isoformat(),
                "solve_method": solve_method,
            },
            pk=("domain", "user_agent", "proxy"),
            coalesce=False,
        )

    def clear(self, domain: str) -> None:
        self.db.execute("DELETE FROM cloudflare_cookies WHERE domain=?", (domain,))


class DataDomeCookieStore:
    """Persist and reuse the solved `datadome` cookie per domain, bound to the
    User-Agent and egress proxy that solved it.

    Primary key is (domain, user_agent, proxy) so the same domain can have
    distinct jars per browser fingerprint / proxy chain. Mirrors CfCookieStore
    but for DataDome's `datadome` cookie (IP + UA bound, stricter than CF).
    """

    def __init__(self, db: "Database"):
        self.db = db

    def get(self, domain: str, *, user_agent: str, proxy: str = "direct") -> dict | None:
        row = self.db.one(
            "SELECT * FROM datadome_cookies WHERE domain=? AND user_agent=? AND proxy=?",
            (domain, user_agent, proxy),
        )
        if row is None:
            return None
        # expiry check via datetime parsing (not ISO string compare — fragile across tz formats)
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        return dict(row)

    def put(
        self,
        domain: str,
        *,
        user_agent: str,
        proxy: str = "direct",
        cookies: list[dict],
        expires_at: str,
        solve_method: str = "browser",
    ) -> None:
        # coalesce=False: a fresh solve MUST overwrite a stale row
        # (db.upsert defaults coalesce=True which would keep the old cookie)
        self.db.upsert(
            "datadome_cookies",
            {
                "domain": domain,
                "user_agent": user_agent,
                "proxy": proxy,
                "cookies": json.dumps(cookies),
                "expires_at": expires_at,
                "solve_method": solve_method,
            },
            pk=("domain", "user_agent", "proxy"),
            coalesce=False,
        )

    def clear(self, domain: str) -> None:
        self.db.execute("DELETE FROM datadome_cookies WHERE domain=?", (domain,))


def prune_all(db: Database, *, keep_days: int, raw_store=None) -> dict:
    """Delete retention-expired rows + raw files, then checkpoint WAL and ANALYZE.

    Timestamp columns per table (see SCHEMA_SQL):
      fetch_log.at, documents.fetched_at, runs.started_at.
    Returns {"fetch_log": n, "documents": n, "runs": n, "raw_files": n}.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    counts: dict = {"raw_files": 0}

    if raw_store is not None:
        # RawStore.prune removes expired files AND the matching documents rows.
        n = raw_store.prune(keep_days)
        counts["documents"] = n
        counts["raw_files"] = n
    else:
        cur = db.execute("DELETE FROM documents WHERE fetched_at < ?", (cutoff,))
        counts["documents"] = cur.rowcount

    cur = db.execute("DELETE FROM fetch_log WHERE at < ?", (cutoff,))
    counts["fetch_log"] = cur.rowcount
    cur = db.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
    counts["runs"] = cur.rowcount

    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.execute("ANALYZE")
    db.conn.commit()
    return counts

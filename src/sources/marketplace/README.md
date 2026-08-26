# G2 Marketplace Scraper

A G2.com company profile and review scraper for the [Signals](../../README.md) sales intelligence platform.

Scrapes product reviews from `https://www.g2.com/products/{slug}/reviews`, extracts structured data
(reviewer, rating, pros/cons, body text, verification status), persists them to SQLite, and exports
to JSON or CSV. Reuses the existing Signals Cloudflare bypass waterfall to handle G2's bot protection.

Disabled by default. Opt-in only. G2's Terms of Service restrict automated scraping — see
[Legal](#legal--ethics) below.

---

## Table of Contents

- [Architecture](#architecture)
- [Data Flow](#data-flow)
- [File Layout](#file-layout)
- [The Parser](#the-parser)
- [The Adapter](#the-adapter)
- [Storage](#storage)
- [Export](#export)
- [Cloudflare Bypass](#cloudflare-bypass)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Testing](#testing)
- [How It Fits in Signals](#how-it-fits-in-signals)
- [Legal & Ethics](#legal--ethics)
- [Limitations & Roadmap](#limitations--roadmap)

---

## Architecture

```
                     Signals Pipeline
                     ================
                          |
                     plan(account)
                          |
                          v
               +----------------------+
               | FetchTask            |
               | source=marketplace_g2|
               | url=g2.com/.../reviews|
               +----------------------+
                          |
                     _fetch_one()
                          |
               +----------+----------+
               | Cloudflare challenge?|
               +----------+----------+
                    yes |     | no
                        v     v
              CloudflareBypass   HTTP GET
              .attempt()         (httpx)
              (5-tier waterfall)      |
                        |             |
                        +------+------+
                               |
                          FetchResult
                               |
                          parse(doc)
                               |
                    +----------+----------+
                    |                     |
                    v                     v
            SignalCandidate         harvest_reviews()
            (intent_2nd_            (G2Review list)
             marketplace)                  |
                    |                      v
                    v              upsert_g2_reviews()
              normalize_batch              (SQLite)
              -> signals table                  |
                                               v
                                         g2_reviews table
                                               |
                                         export_g2_*()
                                               |
                                         v       v
                                       .json    .csv
```

The scraper follows the standard Signals source adapter contract:
`plan() -> [FetchTask]` builds requests, `parse(doc) -> [SignalCandidate]` extracts signals,
and `harvest_reviews() -> [G2Review]` persists raw data to a dedicated table.

---

## Data Flow

1. **Plan** — `MarketplaceG2Source.plan(account, cursor)` checks `account.g2_slug` and builds a
   single `FetchTask` to `https://www.g2.com/products/{g2_slug}/reviews`. If no slug is set, returns
   `[]` (the runner skips the account entirely thanks to `requires = ("g2_slug",)`).

2. **Fetch** — The runner's `_fetch_one()` dispatches the task. Since `marketplace_g2` is a
   browser-tier source and is in `_CF_BYPASS_SOURCES`, any Cloudflare challenge (403 or JS
   interstitial) routes through the 5-tier bypass waterfall. On success, the HTML body is stored
   in the content-addressed raw store (`data/raw/`).

3. **Parse** — `parse(doc, account, task_meta)` decodes the HTML, calls the pure
   `parse_g2_reviews()` function, filters reviews older than 90 days (from `task_meta["today"]`),
   and returns `SignalCandidate` objects with `signal_type="intent_2nd_marketplace"`. Confidence is
   0.85 for verified reviewers, 0.75 otherwise.

4. **Harvest** — `harvest_reviews(doc, account, task_meta)` calls the same parser and returns the
   full list of `G2Review` objects (no date filtering). The runner upserts these to the
   `g2_reviews` SQLite table via `upsert_g2_reviews()`.

5. **Export** — `python -m src.cli g2-export --slug slack` queries the `g2_reviews` table and
   writes `data/exports/g2/slack.json` and `data/exports/g2/slack.csv`.

---

## File Layout

```
src/sources/marketplace/
    g2.py              Pure parser: G2Review dataclass + parse_g2_reviews(html, url)
    collector.py       MarketplaceG2Source adapter: plan/parse/harvest_reviews + upsert_g2_reviews
    __init__.py         Exports MarketplaceG2Source
    README.md           This file

src/export/
    g2_export.py        JSON and CSV exporters (export_g2_json, export_g2_csv, export_g2_all)

src/pipeline/
    runner.py           CF bypass gate (_CF_BYPASS_SOURCES), harvest_reviews hook

src/core/
    models.py           Account.g2_slug field
    db.py               g2_reviews table schema

src/cli.py              g2-export CLI command

config/
    marketplace.yaml    G2-specific options (deep_reviews, max_review_pages, etc.)
    sources.yaml        marketplace_g2 source enable/cadence/rate

tests/
    test_g2_parse.py            Parser unit tests (5 tests)
    test_g2_adapter.py          Adapter plan/parse tests (4 tests)
    test_g2_harvest.py          Upsert + idempotency test (1 test)
    test_g2_export.py           JSON/CSV export tests (2 tests)
    test_g2_slug.py             Account.g2_slug field tests (4 tests)
    test_g2_reviews_db.py       g2_reviews table tests (2 tests)
    test_cli_g2.py              CLI command tests (2 tests)
    test_runner_cf_g2.py        CF bypass routing test (1 test)
    test_g2_e2e.py              End-to-end integration test (1 test)
    fixtures/marketplace/
        g2_reviews.html         Frozen HTML fixture (3 reviews)
```

---

## The Parser

`parse_g2_reviews(html: str, url: str) -> list[G2Review]` in `src/sources/marketplace/g2.py`.

**Pure**: no I/O, no network, no clock, no database. Accepts an already-fetched HTML string and a
URL. Passes the project's [AST purity guard](../techstack/README.md) — no imports of `httpx`,
`requests`, `sqlite3`, or `playwright`; no calls to `datetime.now()` or `date.today()`.

Uses Python's stdlib `html.parser.HTMLParser` (no external dependencies like BeautifulSoup).

### G2Review dataclass

```python
@dataclass
class G2Review:
    review_id: str                    # sha256(slug|reviewer_name|posted_at)[:16]
    product_slug: str                 # e.g. "slack"
    reviewer_name: Optional[str]      # "John D" or "Anonymous User"
    reviewer_title: Optional[str]     # "Software Engineer"
    reviewer_company_size: Optional[str]  # "Mid-Market(51-1000 emp.)"
    rating: Optional[float]           # 0.0–5.0
    review_title: Optional[str]       # "Great collaboration tool"
    review_body: Optional[str]        # Full review text
    pros: list[str]                   # ["Real-time messaging", "Integrations"]
    cons: list[str]                   # ["Notifications can be overwhelming"]
    posted_at: Optional[str]          # ISO date "2026-01-15"
    review_url: Optional[str]         # Source URL
    verified_reviewer: bool           # True if "Verified Reviewer" or "Verified Current User"
    review_source: Optional[str]      # "Organic" or "Invitation from G2 (Original )"
```

### DOM selectors

G2 wraps reviews in `<div class="paper" itemscope itemtype="http://schema.org/Review">` cards (legacy)
or `<div class="review-item">` (newer). The parser handles both.

| Field | Selector | Notes |
|---|---|---|
| reviewer_name | `[itemprop="author"]` (span or div) | If div, strips trailing " Information" noise |
| review_title | `[itemprop="name"]` | |
| review_body | `[itemprop="reviewBody"]` | |
| rating | `div.stars` class `stars-{N}` | N is out of 10; rating = N / 2.0 |
| posted_at | `<time datetime="...">` | ISO date string |
| reviewer_title | `div.mt-4th` (1st) | Inside `div.c-midnight-80` |
| reviewer_company_size | `div.mt-4th` (2nd) | |
| pros | `div[aria-label="Pros"] div.ellipsis` | One per div |
| cons | `div[aria-label="Cons"] div.ellipsis` | One per div |
| verified_reviewer | Text "Verified Reviewer" / "Verified Current User" | Anywhere in card |
| review_source | Text starting with "Review source:" | Prefix stripped |

### review_id generation

```python
review_id = hashlib.sha256(f"{product_slug}|{reviewer_name}|{posted_at}".encode()).hexdigest()[:16]
```

This is the natural key for the `g2_reviews` SQLite table. It deduplicates across collection runs —
the same review fetched twice upserts (updates `last_seen_at`), it doesn't duplicate.

---

## The Adapter

`MarketplaceG2Source(SourceAdapter)` in `src/sources/marketplace/collector.py`.

```python
class MarketplaceG2Source(SourceAdapter):
    key = "marketplace_g2"
    tier = "browser"          # uses BrowserFetcher + Cloudflare bypass
    cadence_hours = 168       # weekly
    requires = ("g2_slug",)   # runner skips accounts without g2_slug
```

### plan(account, cursor) -> list[FetchTask]

Returns one fetch task per account:

```python
FetchTask(
    source="marketplace_g2",
    url=f"https://www.g2.com/products/{account.g2_slug}/reviews",
    domain=account.domain,
    meta={"kind": "reviews", "product_slug": account.g2_slug},
)
```

Returns `[]` if `account.g2_slug` is falsy.

### parse(doc, account, task_meta) -> list[SignalCandidate]

Decodes the HTML, runs `parse_g2_reviews()`, filters reviews older than 90 days from
`task_meta["today"]`, and returns `SignalCandidate` objects:

```python
SignalCandidate(
    signal_type="intent_2nd_marketplace",
    observed_at=posted_at or today_str,
    natural_key=f"g2rev:{product_slug}:{review_id}",
    title=review_title or f"Review by {reviewer_name}",
    summary=review_body[:200],
    url=review_url,
    confidence=0.85 if verified_reviewer else 0.75,
    evidence_data={
        "product_slug": ...,
        "reviewer_name": ...,
        "reviewer_title": ...,
        "rating": ...,
        "pros": [...],
        "cons": [...],
        "review_source": ...,
    },
)
```

These candidates flow into the Signals scoring pipeline as `intent_2nd_marketplace` signals
(category=intent, origin=external, catalyst=primary, degree=2).

### harvest_reviews(doc, account, task_meta) -> list[G2Review]

Same parse, no date filtering. The runner upserts the full list to the `g2_reviews` table.

### upsert_g2_reviews(db, reviews, *, now, raw_ref) -> (new, updated)

Upserts `G2Review` objects into the `g2_reviews` SQLite table. On conflict (same `review_id`),
updates `last_seen_at`, `review_body`, `pros`, `cons`, `rating`, `reviewer_title`,
`reviewer_company_size`, and `raw_ref`. Preserves `first_seen_at`. Returns `(new_count, updated_count)`.

---

## Storage

### g2_reviews SQLite table

```sql
CREATE TABLE g2_reviews (
    review_id            TEXT PRIMARY KEY,   -- sha256(slug|name|date)[:16]
    product_slug         TEXT NOT NULL,
    reviewer_name        TEXT,
    reviewer_title       TEXT,
    reviewer_company_size TEXT,
    rating               REAL,
    review_title         TEXT,
    review_body          TEXT,
    pros                 TEXT,               -- JSON array
    cons                 TEXT,               -- JSON array
    posted_at            TEXT,               -- ISO date
    review_url           TEXT,
    verified_reviewer    INTEGER DEFAULT 0,
    review_source        TEXT,               -- "Organic" | "Invitation from G2"
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    raw_ref              TEXT                -- documents.doc_id for provenance
);
```

Indexes: `product_slug`, `posted_at DESC`.

Raw HTML bodies also live in the content-addressed raw store (`data/raw/<xx>/<sha>.gz`) via the
`Document` / `RawStore` layer, so every review is traceable to its source HTML.

### Account.g2_slug

The `g2_slug` field on the `Account` model (`src/core/models.py`) holds the G2 product slug
(e.g. `"slack"`, `"jira"`, `"salesforce-crm"`). It's seeded manually via CSV:

```csv
domain,name,g2_slug
acme.com,Acme,acme-crm
example.com,Example,slack
```

---

## Export

`src/export/g2_export.py` provides three functions:

### export_g2_json(db, product_slug, export_dir) -> str

Writes `data/exports/g2/{slug}.json` — a JSON array of review dicts, sorted by `posted_at DESC`.
`pros` and `cons` are JSON-decoded back to lists. `verified_reviewer` is a bool.

```json
[
  {
    "review_id": "a1b2c3d4e5f6a7b8",
    "product_slug": "slack",
    "reviewer_name": "John D",
    "reviewer_title": "Software Engineer",
    "reviewer_company_size": "Mid-Market(51-1000 emp.)",
    "rating": 4.5,
    "review_title": "Great collaboration tool",
    "review_body": "Slack has transformed how our team communicates...",
    "pros": ["Real-time messaging", "Integrations with other tools"],
    "cons": ["Notifications can be overwhelming"],
    "posted_at": "2026-01-15",
    "review_url": "https://www.g2.com/products/slack/reviews",
    "verified_reviewer": true,
    "review_source": "Organic",
    "first_seen_at": "2026-08-25T19:44:02+00:00",
    "last_seen_at": "2026-08-25T19:44:02+00:00",
    "raw_ref": "d1"
  }
]
```

Best for DB integration, API consumption, or programmatic analysis.

### export_g2_csv(db, product_slug, export_dir) -> str

Writes `data/exports/g2/{slug}.csv` — one row per review. `pros` and `cons` are semicolon-joined
strings (e.g. `"Real-time messaging; Integrations with other tools"`). Best for spreadsheet analysis.

### export_g2_all(db, export_dir) -> list[str]

Exports JSON + CSV for every product slug that has reviews in the DB.

---

## Cloudflare Bypass

G2 is behind Cloudflare's bot protection. Confirmed during development: `web_extract` failed on
`/products/jira` and `/products/jira/reviews` with Cloudflare challenge pages.

The scraper reuses the existing Signals techstack Cloudflare bypass waterfall
(`src/sources/techstack/cf_bypass.py`). The bypass was generalized from `techstack`-only to also
cover `marketplace_g2` via the `_CF_BYPASS_SOURCES` set in `src/pipeline/runner.py`:

```python
_CF_BYPASS_SOURCES = {"techstack", "marketplace_g2"}
```

### 5-tier waterfall

| Tier | Method | Description |
|---|---|---|
| 1 | Cookie reuse | Replay a previously-solved `cf_clearance` + `__cf_bm` cookie jar (UA + proxy bound) via httpx |
| 2 | Browser solve | Fresh Chromium context (no stale `storage_state`), stealth init script, polls for title change |
| 3 | External solver | 2Captcha/anti-captcha Turnstile token (stub — token-to-cookie injection not fully wired) |
| 4 | Headed fallback | Visible browser, manual or auto-solve (bounded by `headed_solve_timeout_ms`) |
| 5 | Hard stop | Records `cloudflare` as a named observation, invents nothing |

Cookies persist in the `cloudflare_cookies` SQLite table (UA + proxy bound, real expiry from
Playwright cookie `expires`), reused on the next weekly collect.

See [`src/sources/techstack/README.md`](../techstack/README.md) for the full bypass documentation.

---

## Configuration

### config/sources.yaml

```yaml
marketplace_g2:
  enabled: false              # browser tier, opt-in. See config/marketplace.yaml for G2 options
  cadence_hours: 168           # weekly
  rate_per_host: 0.5           # 1 request per 2 seconds
```

### config/marketplace.yaml

```yaml
sites:
  g2:
    enabled: false
    deep_reviews: false        # click "Show More" on each review (slow, browser tier) [planned]
    sign_in_required: false    # full review text requires G2 sign-in (manual cookie) [planned]
    max_review_pages: 5        # cap pagination [planned]
    review_lookback_days: 90   # drop reviews older than this
```

### Environment variables

The Cloudflare bypass reads from `.env`:

```
CLOUDFLARE_SOLVER_PROVIDER=2captcha      # or anticaptcha, or leave blank
CLOUDFLARE_SOLVER_API_KEY=your_key
CLOUDFLARE_BYPASS_STRATEGY=browser_first # browser_first | solver_first | browser_only | disabled
CLOUDFLARE_HEADED_FALLBACK=false         # true to launch visible browser on hard challenges
```

---

## Quick Start

### 1. Seed accounts with g2_slug

```powershell
.\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv
```

`seeds.csv`:
```csv
domain,name,g2_slug
acme.com,Acme,acme-crm
example.com,Example,slack
```

### 2. Enable the source

In `config/sources.yaml`:
```yaml
marketplace_g2:
  enabled: true
  cadence_hours: 168
  rate_per_host: 0.5
```

### 3. Enable the browser tier

In `config/default.yaml`:
```yaml
browser:
  enabled: true
  headless: true
```

### 4. Collect reviews

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source marketplace_g2 --force
```

This fetches the G2 reviews page (routing through the Cloudflare bypass if challenged), parses
the HTML, emits `intent_2nd_marketplace` signals, and upserts raw reviews to the `g2_reviews`
SQLite table.

### 5. Export

```powershell
# Export a specific product
.\.venv\Scripts\python.exe -m src.cli g2-export --slug slack
# -> data/exports/g2/slack.json + data/exports/g2/slack.csv

# Export all products
.\.venv\Scripts\python.exe -m src.cli g2-export

# JSON only
.\.venv\Scripts\python.exe -m src.cli g2-export --slug slack --format json
```

### 6. Query the DB directly

```sql
SELECT reviewer_name, rating, review_title, posted_at
FROM g2_reviews
WHERE product_slug = 'slack'
ORDER BY posted_at DESC;
```

---

## CLI Reference

```
python -m src.cli g2-export [OPTIONS]

  Export G2 reviews to JSON and/or CSV.

Options:
  --slug TEXT     Product slug(s) to export (multiple allowed)
  --dir TEXT      Export directory (default: data/exports/)
  --format TEXT   json | csv | both (default: both)
```

---

## Testing

```powershell
# Run all G2 tests
.\.venv\Scripts\python.exe -m pytest tests/test_g2_parse.py tests/test_g2_adapter.py tests/test_g2_harvest.py tests/test_g2_export.py tests/test_g2_slug.py tests/test_g2_reviews_db.py tests/test_cli_g2.py tests/test_runner_cf_g2.py tests/test_g2_e2e.py -v

# Run purity guard (verifies parser has no forbidden imports/calls)
.\.venv\Scripts\python.exe -m pytest tests/test_source_purity.py -v

# Run full suite
.\.venv\Scripts\python.exe -m pytest -q
```

### Test inventory

| File | Tests | What it covers |
|---|---|---|
| `test_g2_parse.py` | 5 | Parser extracts all fields, handles anonymous reviewers, empty HTML |
| `test_g2_adapter.py` | 4 | plan() requires g2_slug, returns correct URL; parse() returns candidates, filters old reviews |
| `test_g2_harvest.py` | 1 | upsert_g2_reviews persists, idempotent (no duplicates on re-run) |
| `test_g2_export.py` | 2 | JSON export decodes pros/cons to lists; CSV export flattens to semicolon strings |
| `test_g2_slug.py` | 4 | Account.g2_slug field, to_db_row, from_db_row |
| `test_g2_reviews_db.py` | 2 | g2_reviews table exists with all columns; upsert round-trip |
| `test_cli_g2.py` | 2 | g2-export command exists, creates JSON + CSV files |
| `test_runner_cf_g2.py` | 1 | Cloudflare bypass routes marketplace_g2 tasks |
| `test_g2_e2e.py` | 1 | Full pipeline: plan -> parse -> harvest -> export -> idempotent upsert |

### Fixture

`tests/fixtures/marketplace/g2_reviews.html` — a frozen HTML page with 3 review cards:

| # | Reviewer | Rating | Date | Verified | Source |
|---|---|---|---|---|---|
| 1 | John D (span) | 4.5 (stars-9) | 2026-01-15 | Yes | Organic |
| 2 | Jane S (span) | 5.0 (stars-10) | 2025-12-03 | Yes | Invitation from G2 |
| 3 | Anonymous User (div) | 3.5 (stars-7) | 2025-11-20 | Yes | Organic |

Review 3 uses `<div itemprop="author">` instead of `<span>` to test both code paths.

---

## How It Fits in Signals

Signals is a sales signal enrichment engine that collects buying signals about target accounts
from ~20 zero-cost sources, resolves them to an identity graph, scores and tiers them, and exports
ranked account briefs.

The G2 scraper is one source adapter. It contributes:

- **Signal**: `intent_2nd_marketplace` (category=intent, origin=external, catalyst=primary, degree=2,
  weight=26, half_life=30 days) — a second-degree marketplace intent signal. A verified review of
  a product in your space from someone at a target account is a buying signal.

- **Raw data**: `g2_reviews` table — the full review text, pros/cons, rating, and reviewer metadata
  for analysis beyond signal scoring.

- **Export**: JSON and CSV files for integration with CRM, spreadsheets, or downstream pipelines.

The adapter is disabled by default (`enabled: false` in `config/sources.yaml`). It's opt-in because:
1. G2 ToS restrict automation
2. It requires the browser tier (Playwright + Chromium)
3. It may trigger Cloudflare challenges

---

## Legal & Ethics

- **G2 ToS**: G2's Terms of Service restrict automated scraping. This adapter is disabled by
  default. Enable it at your own risk. The Signals README's Legal section already notes:
  "G2 / Capterra / LinkedIn ToS restrict automation — those adapters stay disabled."

- **Cloudflare bypass**: The bypass solves JS/managed challenges to read public review pages. It
  does not bypass authentication, paywalls, or login-gated content. It is scoped to `techstack`
  and `marketplace_g2` only — other sources retain the 403 hard-stop.

- **No auth bypass**: There is no automated login. Sign-in for full review text (if needed) is a
  manual, opt-in flow where the user provides a G2 session cookie. No password storage, no
  automated login form submission.

- **Rate limiting**: The adapter uses a 0.5 req/sec rate limit and a 168-hour (weekly) cadence.
  Polite HTTP. A real contact address in the User-Agent.

- **No reselling**: The raw store is a local reproducibility cache. Never resell raw content.

---

## Limitations & Roadmap

### Current limitations

- **Single page**: The adapter fetches one reviews page per account per collect run. Multi-page
  pagination (`?page=2`, `?page=3`, ...) is not yet implemented (config option `max_review_pages`
  exists but is not wired).
- **Truncated reviews**: G2 truncates long review bodies with a "Show More" link. The current
  parser captures the truncated text. Full text requires clicking "Show More" (planned) or
  sign-in (planned).
- **Sign-in required for full text**: Some review sections ("What do you dislike?",
  "Recommendations to others") may require G2 sign-in to view. No automated login.
- **DOM fragility**: G2 obfuscates class names and changes structure over time. The parser is
  tested against a frozen fixture; real-world G2 may differ. Selectors may need maintenance.
- **Cloudflare escalation**: G2 may use managed/Turnstile challenges. The browser-only JS solve
  may fail. The 2Captcha solver tier is a stub (token-to-cookie injection not fully wired). Headed
  fallback is available but requires manual intervention.

### Planned (optional tasks from the implementation plan)

- **Task 9**: Switch parser to BeautifulSoup4 for more robust DOM handling
- **Task 11**: "Show More" deep-review mode — click to expand truncated reviews via browser tier
- **Task 12**: G2 sign-in cookie support — manual cookie file for full review text (no automated login)
- **Multi-page pagination**: Follow `?page=N` links up to `max_review_pages`
- **Auto-resolve g2_slug**: Resolve product slug from company name via G2 search (currently manual)

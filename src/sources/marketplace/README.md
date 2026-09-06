# Marketplace review sources (G2 / Capterra / TrustRadius / Software Advice / GetApp)

Marketplace review scrapers for the [Signals](../../README.md) sales intelligence platform: G2 (browser-tier DataDome), Capterra and TrustRadius (server-rendered HTTP behind Cloudflare), and the Gartner-network pair Software Advice / GetApp (server-rendered JSON-LD via the standard fetcher).

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
- [DataDome Protection](#datadome-protection)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [Capterra](#capterra)
- [TrustRadius](#trustradius)
- [CLI Reference](#cli-reference)
- [Testing](#testing)
- [How It Fits in Signals](#how-it-fits-in-signals)
- [Legal & Ethics](#legal--ethics)
- [Features](#features)
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

The current G2 page is a client-side React SPA: the plain `/reviews` page is only the app shell,
and the actual review cards are served as **client-rendered `elv-*` DOM** from the
`GET /products/{slug}/reviews_and_filters` HTML fragment (reverse-engineered — see the note
below). There is no JSON API, no `__NEXT_DATA__`, and no `/_next/data` route. Pagination and
sorting are plain `?page=N` and `?sort=...` query parameters that work on **both** the full
`/reviews` URL and the `reviews_and_filters` fragment URL.

1. **Plan** — `MarketplaceG2Source.plan(account, cursor)` checks `account.g2_slug` and builds a
   single `FetchTask` to `https://www.g2.com/products/{g2_slug}/reviews`. If no slug is set, returns
   `[]` (the runner skips the account entirely thanks to `requires = ("g2_slug",)`).
   `g2_reviews_url(slug, page=N, sort=...)` builds the full-page URL; pagination/sort is optional
   query-string state, not a separate endpoint.

2. **Fetch** — The runner's `_fetch_one()` dispatches the task. `marketplace_g2` is a browser-tier
   source, so for G2 tasks the runner first calls `_fetch_g2_fragment(task)`: when a DataDome
   stealth (Patchright) browser is available it fetches the **rendered** `reviews_and_filters`
   fragment (via `g2_reviews_fragment_url(slug, page=...)`) so the `Document` body contains the
   real reviews — the `/reviews` shell alone has none. If no stealth browser is present, it falls
   back to the normal challenge→bypass→curl flow (Cloudflare 5-tier waterfall for
   `_CF_BYPASS_SOURCES`, then DataDome bypass). The returned HTML body is stored in the
   content-addressed raw store (`data/raw/`).

3. **Parse** — `parse(doc, account, task_meta)` decodes the HTML and **format-detects** the body:
   live `elv-*` DOM → the pure `extract_g2_reviews()`; legacy `itemprop` microdata →
   `parse_g2_reviews()` (backward-compat fallback). It then filters reviews older than 90 days
   (from `task_meta["today"]`) and returns `SignalCandidate` objects with
   `signal_type="intent_2nd_marketplace"`. Confidence is 0.85 for verified reviewers, 0.75
   otherwise.

4. **Harvest/Follow** — `harvest_reviews(doc, account, task_meta)` runs the same format-detecting
   parse and returns the full list of `G2Review` objects (no date filtering). The runner upserts
   these to the `g2_reviews` SQLite table via `upsert_g2_reviews()`. `follow_tasks()` plans the
   next page via `g2_reviews_url(slug, page=N)` up to `max_review_pages`, so a single collect run
   sweeps multiple pages per account.

5. **Export** — `python -m src.cli g2-export --slug slack` queries the `g2_reviews` table and
   writes `data/exports/g2/slack.json` and `data/exports/g2/slack.csv`.

---

## File Layout

```
src/sources/marketplace/
    g2.py              Pure parser: G2Review dataclass + extract_g2_reviews (current elv-* DOM),
                       parse_g2_reviews (legacy itemprop fallback), g2_reviews_url /
                       g2_reviews_fragment_url URL builders, filter_deep_reviews
                       (deep-reviews extreme-rating bound)
    base.py            MarketplaceReview normalizing base — safe rating coercion
                       (str/int/None/junk -> float, never raises) shared by the parsers
    deep_reviews.py    Deep-reviews bounding helpers: should_expand / normalize_bound
                       ('extreme' default; explicit null = legacy bound-free)
    collector.py       MarketplaceG2Source adapter: plan/parse/harvest_reviews + upsert_g2_reviews
                       (adopts legacy capterra rows on the natural key when a legacy row exists)
    capterra.py        Capterra parser (server-rendered cards; g2_reviews rows with source='capterra';
                       review_id hashes the ISO-normalized posted date, OneTrust consent tolerant)
    trustradius.py     TrustRadius parser (0-10 → 0-5 rating normalization; source='trustradius')
    sa_ga.py           Software Advice / GetApp parsers + adapters (JSON-LD SSR; keys
                       marketplace_softwareadvice / marketplace_getapp; reviews land in g2_reviews
                       via the runner's generic marketplace dispatch; disabled by default, opt-in
                       via extra_data.sa_url / extra_data.getapp_url)
    trend.py           marketplace_review_trend — review count/rating deltas vs prior cycle
    empty_log.py       EmptyLog — per-slug consecutive-empty-cycle bookkeeping (drives backoff)
    selfcheck.py       Thin per-vendor wrappers over the shared five-state runner
                       (src/core/selfcheck.py owns run_source_selfcheck)
    __init__.py         Exports MarketplaceG2Source
    README.md           This file

src/export/
    g2_export.py        JSON and CSV exporters (export_g2_json, export_g2_csv, export_g2_all)

src/pipeline/
    runner.py           CF bypass gate (_CF_BYPASS_SOURCES), harvest_reviews hook,
                        _fetch_g2_fragment() fetches rendered reviews_and_filters fragment

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
        g2_reviews.html              Frozen legacy itemprop fixture (3 reviews)
        g2_reviews_live_sierra.html  Frozen live elv-* fragment (sierra)
        g2_reviews_live_helcim.html  Frozen live elv-* fragment (helcim)
```

---

## The Parser

`src/sources/marketplace/g2.py` ships two pure parsers for the two G2 DOM shapes:

- **`extract_g2_reviews(html: str, product_slug: str) -> list[G2Review]`** — the **current**
  parser. Reads G2's live, client-rendered `elv-*` review cards (served from the
  `reviews_and_filters` fragment). This is what `parse()` / `harvest_reviews()` / `follow_tasks()`
  use on live data.
- **`parse_g2_reviews(html: str, url: str) -> list[G2Review]`** — the **legacy** parser, kept for
  backward compatibility with the historical `itemprop` schema.org microdata and the frozen
  `g2_reviews.html` test fixture. The adapter format-detects the body (`elv-stars` / `five-star-rater`
  / `…-review-<digits>` markers) and falls back to this when no `elv-*` markers are present.

Both are **pure**: no I/O, no network, no clock, no database. They accept an already-fetched HTML
string and return structured data. Both pass the project's
[AST purity guard](../../../tests/test_source_purity.py) — no imports of `httpx`, `requests`, `sqlite3`, or
`playwright`; no calls to `datetime.now()` or `date.today()`.

Both use BeautifulSoup4 (`bs4`) with the `lxml` parser for robust DOM handling. The `bs4` import is
deferred to call time (inside each parser) so the module remains import-pure for the
[AST purity guard](../../../tests/test_source_purity.py) — the guard scans module-level imports only.

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
    nps_score: Optional[int]          # None — no per-card markup in current DOM
    helpful_votes: Optional[int]      # None — no per-card markup in current DOM
```

### DOM selectors (`extract_g2_reviews` — current elv-\* DOM)

The live review cards are `<article id="{slug}-review-<digits>" ue="track-in-viewport">` elements
in the rendered `reviews_and_filters` fragment. All selectors below are matched within each card:

| Field | Selector | Notes |
|---|---|---|
| reviewer_name | `div.elv-font-bold` | First bold block in the card |
| reviewer_title | `div.elv-text-xs` (1st) | |
| reviewer_company_size | `div.elv-text-xs` (2nd) | |
| review_title | `div.elv-text-lg.elv-font-bold` / `div.elv-text-lg` | Leads/trailing quotes stripped |
| review_body | `p` sibling of "What do you like best…" heading | G2 attribution boilerplate stripped |
| pros | `p` sibling of "What do you like best…" heading | One item |
| cons | `p` sibling of "What do you dislike…" heading | One item |
| rating | `.elv-star-wrapper__desc__rating` text `N / 5`; else `div.elv-stars` `elv-stars-{N}` | 0–5 float |
| posted_at | `<meta content="YYYY-MM-DD">` | First ISO-date meta in card |
| review_url | `[data-clipboard-text]` | Link-share URL; falls back to `/reviews` page |
| review_id | from `article id="…-review-{digits}"` | Fallback `sha256(slug\|name\|date)[:16]` |
| verified_reviewer | "validated/verified current user" etc. | Token match anywhere in card |
| review_source | `.elv-status-badge__label` | "Source: …" / "Incentivized Review" |

### DOM selectors (`parse_g2_reviews` — legacy itemprop)

G2 wraps legacy reviews in `<div class="paper" itemscope itemtype="http://schema.org/Review">`
cards (or `<div class="review-item">`). `reviewer_name` comes from `[itemprop="author"]`,
`review_title` from `[itemprop="name"]`, `review_body` from `[itemprop="reviewBody"]`, `rating`
from `div.stars` class `stars-{N}` (N out of 10, rating = N / 2.0), `posted_at` from
`<time datetime="...">`, pros/cons from `div[aria-label="Pros"/"Cons"] div.ellipsis`, verification
from the "Verified Reviewer"/"Verified Current User" text, and `review_source` from the
"Review source:" label.

### review_id generation

```python
# posted_at is ISO-normalized before hashing (capterra.py): month-name parser
# first, to_iso_date fallback, raw rendered string only when unparseable — so
# the same review rendered with different date formats keeps the same id.
review_id = hashlib.sha256(f"{product_slug}|{reviewer_name}|{posted_iso}".encode()).hexdigest()[:16]
```

This is the natural key for the `g2_reviews` SQLite table. It deduplicates across collection runs —
the same review fetched twice upserts (updates `last_seen_at`), it doesn't duplicate. Since the P3
normalization change, `upsert_g2_reviews` also **adopts legacy rows**: if no row matches the new
id but a capterra row with the same (product_slug, reviewer_name, posted_at) exists under the old
raw-date id scheme, it is updated in place (id rewritten, `first_seen_at` preserved) instead of
duplicating.

### Reverse-engineered data path (recorded for cheap rebuilds)

Confirmed by the discovery spike (Aug 2026): G2 is a client-side React SPA. The review data is
**not** a JSON API, not `__NEXT_DATA__`, and not a `/_next/data` route. Reviews are served as
**client-rendered `elv-*` DOM from `GET https://www.g2.com/products/{slug}/reviews_and_filters`**
(a server-rendered HTML fragment the SPA injects into the `/reviews` page, which is only the app
shell). Key facts for rebuilding without repeating the probe:

- **Endpoint**: `GET /products/{slug}/reviews_and_filters` — returns the rendered review DOM in
  the response body. Fetch it through a JS-capable browser (the DataDome stealth/Patchright tier);
  a plain HTTP GET also works when a valid `datadome` cookie is present.
- **Card structure**: `<article id="{slug}-review-<digits>" ue="track-in-viewport">`, with
  `elv-*` component classes (`elv-font-bold`, `elv-text-xs`, `elv-text-lg`, `elv-stars-{N}`,
  `elv-star-wrapper__desc__rating`, `elv-status-badge__label`) and
  `data-controller="five-star-rater"` widgets.
- **Pagination / sort**: plain `?page=N` and `?sort=...` query params, honored on **both** the
  full `/reviews` URL and the `reviews_and_filters` fragment URL. No buildId, no cursor.
- **Field mapping**: see the `extract_g2_reviews` selector table above; rating is a 0–5 float
  (from the `N / 5` rating text or `elv-stars-{N}` where N is out of 10), `posted_at` is the first
  ISO `YYYY-MM-DD` `<meta content>` in the card, `review_url` comes from `[data-clipboard-text]`,
  and verification is a token match for "validated/verified current user".
- **Test fixtures**: `tests/fixtures/marketplace/` holds both the live `elv-*` fragments
  (`g2_reviews_live*.html`) and the legacy `itemprop` page (`g2_reviews.html`) that keeps
  `parse_g2_reviews` coverage green.

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

Decodes the HTML, format-detects the body (live `elv-*` DOM → `extract_g2_reviews()`, legacy
`itemprop` → `parse_g2_reviews()` fallback), filters reviews older than 90 days from
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
    nps_score            INTEGER,            -- None until real per-card markup exists
    helpful_votes        INTEGER,            -- None until real per-card markup exists
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
_CF_BYPASS_SOURCES = {"techstack", "marketplace_g2", "marketplace_capterra", "marketplace_trustradius"}
```

### 5-tier waterfall

| Tier | Method | Description |
|---|---|---|
| 1 | Cookie reuse | Replay a previously-solved `cf_clearance` + `__cf_bm` cookie jar (UA + proxy bound) via httpx |
| 2 | Browser solve | Fresh Chromium context (no stale `storage_state`), stealth init script, polls for title change |
| 3 | External solver | 2Captcha/anti-captcha Turnstile token → `inject_turnstile_token` sets `cf_clearance` in the browser and polls for clearance |
| 4 | Headed fallback | Visible browser, manual or auto-solve (the `headed_solve_timeout_ms` config field is currently advisory — the effective bound is `solve_timeout_ms`) |
| 5 | Hard stop | Records `cloudflare` as a named observation, invents nothing |

Cookies persist in the `cloudflare_cookies` SQLite table (UA + proxy bound, real expiry from
Playwright cookie `expires`), reused on the next weekly collect.

See [`src/sources/techstack/README.md`](../techstack/README.md) for the full bypass documentation.

---

## DataDome Protection

G2 is behind **DataDome** in addition to Cloudflare. DataDome uses TLS fingerprinting
(JA3/JA4), HTTP/2 fingerprinting, browser fingerprinting, IP reputation, and behavioral
analysis to detect bots — stricter than Cloudflare's checks. Unlike Cloudflare, DataDome
detects Playwright automation, so the browser solve tier that works for CF cannot clear DD.

The `DataDomeBypass` waterfall (`src/sources/techstack/datadome_bypass.py`) runs after the
Cloudflare bypass — if the response is still a DataDome challenge (403 or 200 with
`captcha-delivery.com` in the body), the DD bypass takes over. Detection is content-based
(`is_datadome_challenge` in `src/sources/techstack/datadome.py`), not gated by source.

### 5-tier waterfall

| Tier | Method | Description |
|---|---|---|
| 1 | Cookie reuse | Replay a previously-solved `datadome` cookie (domain + UA + proxy bound) from the `datadome_cookies` SQLite table via httpx |
| 2 | curl_cffi TLS impersonation | HTTP GET with `curl_cffi` using `impersonate="chrome"` — matches a real browser's JA3/JA4 fingerprint. Many DD challenges clear here with no CAPTCHA solve |
| 2.5 | Patchright stealth browser | Undetected Chromium with behavioral warm-up — navigates to the G2 homepage first (scroll, dwell), then to the target page. Clears DataDome's `rt='i'` interstitial device check that curl_cffi can't (requires JS execution). Patchright patches Chromium at the C++ level (not JS injection) to remove `navigator.webdriver`, the `Runtime.enable` CDP leak, and `--enable-automation` flags |
| 3 | External solver | 2Captcha/CapSolver `DataDomeSliderTask` — returns a `datadome` cookie directly (set in the HTTP client's cookie jar, no browser re-entry needed) |
| 4 | Headed fallback | Visible browser, manual or auto-solve (the `headed_solve_timeout_ms` config field is currently advisory — the effective bound is `solve_timeout_ms`) |
| 5 | Hard stop | Records `datadome` as a named observation, invents nothing |

**Key difference from Cloudflare**: The DataDome solver returns a **cookie**, not a token.
The cookie is set directly in the HTTP client's cookie jar — no browser re-entry needed
(unlike CF Turnstile, which needs a browser to convert token → `cf_clearance` cookie).

Cookies persist in the `datadome_cookies` SQLite table (`DataDomeCookieStore` in
`src/core/db.py`), bound to domain + User-Agent + proxy, reused on the next weekly collect.

### Empirical findings (Aug 2026 live testing)

- **The DataDome clearance cookie is fingerprint-bound to the browser session that solved
  it.** Replay through any HTTP tier — even one with byte-exact Chrome TLS + HTTP/2
  fingerprints (`src/antibot/`) — still returns the 403 interstitial. DataDome validates the
  JS probe payload behind the cookie, not just network identity. Therefore **G2 collection
  renders and parses inside the headed browser** (no HTTP-tier refetch).
- **Headed + warm-up is what clears it.** The runner forces the stealth browser headed and
  runs a behavioral warm-up (homepage → scroll → dwell) before the fragment navigation.
  Headless always gets the `rt='i'` interstitial; headed clears consistently.
- **Empty ≠ blocked.** Products with no reviews return a real 200 page with zero review
  cards (~215KB shell, no `elv-stars`) — logged as `empty`, distinct from a challenge.
- **Pacing still matters.** Repeated rapid browser launches escalate DataDome into blocking
  whole sessions. Keep live collection paced (the runner's built-in warm-up + per-page flow
  is tuned for this; avoid tight custom loops).

### Residential proxy requirement

A **residential proxy is required for the solver tier** (Tier 3). DataDome bans datacenter
IPs immediately with `t=bv` (IP banned) — no captcha solve will work from a banned IP.
The `t=fe` parameter means the captcha is solvable.

- Set `DATADOME_RESIDENTIAL_PROXY` in `.env` (e.g. `http://user:pass@gate.provider.com:8000`)
- Use a **sticky session** proxy — the `datadome` cookie is IP-bound, so IP rotation between
  the solve and the re-fetch invalidates it
- **Optional for local use**: Tiers 1–2 (cookie reuse, curl_cffi) may work without a proxy.
  The proxy is only needed when the solver tier is invoked

### Cost

~$1.45 per 1000 DataDome solves (2Captcha). At weekly cadence for 30 companies (1 page each),
that's ~30 solves/week ≈ $0.04/week. CapSolver is also supported but typically more expensive.

The solver is invoked only when Tiers 1–2 fail, so most weeks incur zero solver cost if cached
cookies and TLS impersonation suffice.

### DataDome vs Cloudflare

| Aspect | Cloudflare | DataDome |
|---|---|---|
| Detection | JS challenge page ("Just a moment") | Slider CAPTCHA / interstitial |
| Cookie | `cf_clearance` | `datadome` |
| Cookie binding | UA + proxy | UA + proxy + IP (strict) |
| TLS fingerprint check | Yes (JA3/JA4) | Yes (JA3/JA4) — stricter |
| Browser solves it? | Yes (Playwright can solve JS) | No (detects Playwright automation) |
| Solver returns | Token (needs browser to set cookie) | Cookie directly (set in HTTP jar) |
| Proxy requirement | Optional | **Required** (residential) |
| Cost per 1000 | ~$2.99 (Turnstile) | ~$1.45 (DataDome slider) |

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
    deep_reviews: false        # click "Show More" on each review (slow, browser tier)
    deep_reviews_bound: extreme  # P3: expand only ratings >=4.0 or <=2.0; explicit null = legacy bound-free
    sign_in_required: false    # full review text requires G2 sign-in (manual cookie)
    max_review_pages: 5        # cap pagination via follow_tasks
    review_lookback_days: 90   # drop reviews older than this (config-driven)
    session_cookie_file: null  # path to a JSON cookie file for G2 sign-in (no automated login)
                               # NOTE: the committed g2 site entry ships `session_cookie_file: data/g2_cookies.json` —
                               # a local cookie-export path. Set it to `null` unless you are deliberately reusing
                               # your own exported G2 session cookies, and NEVER commit a real cookie file.
```

### Environment variables

The Cloudflare bypass reads from `.env`:

```
CLOUDFLARE_SOLVER_PROVIDER=2captcha      # or anticaptcha, or leave blank
CLOUDFLARE_SOLVER_API_KEY=your_key
CLOUDFLARE_BYPASS_STRATEGY=browser_first # browser_first | solver_first | browser_only | disabled
CLOUDFLARE_HEADED_FALLBACK=false         # true to launch visible browser on hard challenges
```

The DataDome bypass reads from `.env`:

```
DATADOME_SOLVER_PROVIDER=2captcha        # 2captcha | capsolver
DATADOME_SOLVER_API_KEY=your_key          # your 2captcha/capsolver API key
DATADOME_RESIDENTIAL_PROXY=http://user:pass@gate.provider.com:8000  # required for solver tier
```

### config/default.yaml — DataDome block

```yaml
datadome:
  enabled: true                 # bypass layers for marketplace_g2 (default.yaml ships it on)
  bypass_strategy: solver       # solver | curl_cffi_first | disabled
  solver_provider: null         # 2captcha | capsolver | null
  solver_api_key: null          # set via DATADOME_SOLVER_API_KEY env var
  headed_fallback: false        # launch visible browser on hard challenges
  cookie_ttl_hours: 24          # datadome cookies are short-lived
  residential_proxy: null       # required for solver — set via DATADOME_RESIDENTIAL_PROXY env var
```

| Config key | Env var | Default | Description |
|---|---|---|---|
| `datadome.enabled` | — | `true` | Master switch for the DataDome bypass layers (live targets additionally need the solver env vars) |
| `datadome.bypass_strategy` | — | `solver` | `solver` \| `curl_cffi_first` \| `disabled` |
| `datadome.solver_provider` | `DATADOME_SOLVER_PROVIDER` | `null` | `2captcha` \| `capsolver` \| `null` |
| `datadome.solver_api_key` | `DATADOME_SOLVER_API_KEY` | `null` | Solver API key (prefer env var) |
| `datadome.headed_fallback` | — | `false` | Launch visible browser on hard challenges (Tier 4) |
| `datadome.cookie_ttl_hours` | — | `24` | Cookie TTL ceiling for cached `datadome` cookies |
| `datadome.residential_proxy` | `DATADOME_RESIDENTIAL_PROXY` | `null` | Residential proxy URL — **required for solver tier** |

---

## Quick Start

> **Prerequisite for live G2 collection:** the DataDome solver env vars
> (`DATADOME_SOLVER_PROVIDER`, `DATADOME_SOLVER_API_KEY`,
> `DATADOME_RESIDENTIAL_PROXY` — see *DataDome protection* below and
> `.env.example`). Capterra/TrustRadius/SA/GetApp need no solver.

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

If you don't know a product's G2 slug, auto-resolve it from the company name:

```powershell
.\.venv\Scripts\python.exe -m src.cli resolve --g2
```

This runs the G2 search resolver (`src/identity/g2_resolve.py`) for every account that lacks a
`g2_slug`, looks up the product page, and writes the slug back to the account record. Accounts
without a slug are skipped by the collector, so resolving first avoids empty runs.

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
  headless: true        # ignored for G2 fragments — the runner forces headed (see DataDome Protection)
```

### 4. Collect reviews

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source marketplace_g2 --force
```

This fetches the **rendered `reviews_and_filters` fragment through the stealth browser forced
headed with a behavioral warm-up** (homepage visit + scroll + dwell — DataDome only clears headed,
and its clearance cookie is fingerprint-bound to the browser session, so an HTTP-tier refetch of
the harvested cookie does not work), classifies the fragment (`ok` / `empty` / `challenge`),
parses the HTML, emits `intent_2nd_marketplace` signals, and upserts raw reviews to the
`g2_reviews` SQLite table. Pages 2..N (via `follow_tasks`) skip the warm-up; successful fetches
feed the anti-bot `RouteState` so per-domain cookie lifetimes are learned automatically.

**Log lines to know:**
- `g2 slug X has zero reviews (new/quiet product) — not a block` → INFO, expected for new products
- `g2 fragment ... is a DataDome challenge — session cookies likely stale; re-export
  data/g2_cookies.json from a logged-in browser` → WARNING: re-export cookies and re-run
- `skipping warm-up (cookies known-stale)` → anti-bot routing has learned the cookies are dead

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

## Capterra

Capterra (Gartner) reviews are scraped by `marketplace_capterra`, mirroring the
G2 architecture. Built against the Task-1 discovery spike (Aug 2026).

- **URL structure:** `https://www.capterra.com/p/<numeric-id>/<Slug>/reviews/`.
  The numeric product id is part of the URL and **not derivable from the slug
  alone**, so the account field stores the full segment, e.g. `19319/JIRA`
  (Jira), `211559/Trello`, `56808/Basecamp`. Comma-separated multi-segments fan
  out like G2 slugs; pagination is `?page=N` on the same URL (max 3 pages).
- **Rendering model:** reviews are **server-rendered** (Next.js RSC HTML) —
  unlike G2, no stealth browser is needed for parsing; the plain
  `CurlCffiFetcher` TLS-impersonated fetch works against the rendered HTML.
- **Anti-bot:** **Cloudflare Bot Management** (CF-only per the spike — no
  DataDome observed). Datacenter IPs are blocked; the residential-IP + browser
  UA path clears it. Challenge detection covers both DataDome bodies and CF
  markers (`Just a moment`, `cf-chl`).
- **Card DOM:** review cards sit in `div[data-test-id='review-cards-container']`;
  the overall rating is `div[data-testid='Overall Rating-rating']`. The parser
  (`extract_capterra_reviews`) is pure (deferred bs4, no I/O, no clock).
- **Persistence:** reviews share the `g2_reviews` table with G2. A `source`
  column (`TEXT DEFAULT 'g2'`, NEW_COLUMNS migration) records provenance:
  `upsert_capterra_reviews` writes `source='capterra'` rows; G2 rows keep
  `source='g2'`. Capterra `review_id` is a sha256 hash of
  (slug, reviewer, posted) while G2 ids are raw numeric survey ids — no
  collision risk. Natural keys use the `caprev:` prefix.
- **Config:** `config/marketplace.yaml` → `sites.capterra`
  (`enabled: false` by default, `max_review_pages: 3`,
  `review_lookback_days: 90`).
- **Self-check:** `.venv\Scripts\python.exe -m src.cli capterra-selfcheck
  --slug 19319/JIRA` — same five states as `g2-selfcheck` (ok/drift/empty/
  challenge/error), but via the plain HTTP fetcher (no browser window). Drift
  = the review-cards container is present but the parser extracted 0. Exit 0
  only on `ok`/`empty`. A OneTrust consent banner alongside valid cards is
  **not** drift — the result detail notes "consent-banner present" and the
  state stays `ok`.
- **Pacing:** keep ≥4s between requests to capterra.com; the self-check makes
  a single request.
- **Legal & ethics:** same posture as G2 — Capterra (Gartner) ToS restrict
  automated collection. The source is **disabled by default and opt-in**;
  enable deliberately, pace requests, and only collect what the pipeline needs.

---

## TrustRadius

TrustRadius reviews are scraped by `marketplace_trustradius`, mirroring the
G2/Capterra architecture. Built against a live capture (Aug 2026, one paced
curl_cffi chrome-impersonation request to
`https://www.trustradius.com/products/slack/reviews` — HTTP 200, no challenge).

- **URL structure:** `https://www.trustradius.com/products/<slug>/reviews`.
  Unlike Capterra, the slug alone is the URL key (no numeric id). Comma-separated
  multi-slugs fan out like G2; `follow_tasks` paginates with the conventional
  `?page=N` query param (the live page's pagination widget is client-side and
  renders no `?page` links, but the query param is the standard contract; max
  2 pages by default).
- **Rendering model:** reviews are **server-rendered** (Next.js) — the plain
  TLS-impersonated fetch works against the rendered HTML, like Capterra.
- **Anti-bot:** Cloudflare-fronted (the probe reached the app server; no CF
  challenge was served to the chrome-impersonated client). The source is in
  `_CF_BYPASS_SOURCES`, so CF challenges fall to the bypass waterfall. Challenge
  detection covers DataDome bodies plus CF markers (`Just a moment`, `cf-chl`).
- **Card DOM:** review cards are `<article class="Review_review__5RC6b">`
  elements (hash suffix NEVER selected) containing
  `div[data-testid='stars-container']` + `div[data-testid='content']`. Rating
  is `data-rating` on a **0–10 scale**, normalized to 0–5. Dates come from
  `<time datetime>`; the per-review id is the `/reviews/<slug>` URL tail.
  The parser (`extract_trustradius_reviews`) is pure (deferred bs4, no I/O,
  no clock).
- **Persistence:** reviews share the `g2_reviews` table; `upsert_trustradius_reviews`
  writes `source='trustradius'` rows. Natural keys use the `trrev:` prefix
  (`trrev:<slug>:<review-id>`). No NPS/helpful-vote markup exists in the
  current card DOM — those fields are `None`.
- **Config:** `config/marketplace.yaml` → `sites.trustradius` (`enabled: false`
  by default, `max_review_pages: 2`, `review_lookback_days: 90`).
- **Self-check:** `run_selfcheck(fetcher, slug='slack', source='trustradius')` —
  same five states as capterra (ok/drift/empty/challenge/error) via the plain
  HTTP fetcher. Drift = review-card markup present but 0 parsed. There is no
  dedicated CLI command yet (a `trustradius-selfcheck` command is a roadmap
  item); the fetcher needs a small shim — copy the `_CurlShim` adapter from
  the `capterra-selfcheck` command in `src/cli.py` and pass
  `run_selfcheck(shim, slug="slack", source="trustradius", config=cfg)`.
  (The underlying five-state runner is the shared `src/core/selfcheck.py`
  implementation since the T25 extraction.)
- **Pacing:** keep ≥4s between requests to trustradius.com; the self-check
  makes a single request.
- **Legal & ethics:** same posture as G2/Capterra — ToS restrict automated
  collection. Disabled by default and opt-in.

---

## Testing

```powershell
# Run all G2 tests
.\.venv\Scripts\python.exe -m pytest tests/test_g2_extract.py tests/test_g2_parse.py tests/test_g2_adapter.py tests/test_g2_harvest.py tests/test_g2_export.py tests/test_g2_slug.py tests/test_g2_reviews_db.py tests/test_cli_g2.py tests/test_runner_cf_g2.py tests/test_runner_g2.py tests/test_g2_e2e.py -v

# Run purity guard (verifies parser has no forbidden imports/calls)
.\.venv\Scripts\python.exe -m pytest tests/test_source_purity.py -v

# Run full suite
.\.venv\Scripts\python.exe -m pytest -q

# Selector-drift self-check (live, headed browser — one known-good slug)
.\.venv\Scripts\python.exe -m src.cli g2-selfcheck --slug sierra
```

### Self-check (`g2-selfcheck`)

> **Post-P2 note:** the five-state runner now lives in
> `src/core/selfcheck.py` (`run_source_selfcheck`) and is shared across
> sources; `marketplace/selfcheck.py` keeps thin per-vendor wrappers, so the
> states/URLs/extractors below are unchanged. A generic CLI
> (`selfcheck --source techstack|news_rss|ats_greenhouse`) covers
> *non-marketplace* sources only — marketplace keeps its dedicated
> `g2-selfcheck` / `capterra-selfcheck` commands (browser tier + DataDome).

A live selector-drift alarm for the `elv-*` parser. The command fetches one
known-good product's reviews_and_filters fragment through the stealth browser
(headed — DataDome blocks headless) and verifies the parser still extracts
reviews. **Run it after any G2 parser change, or if a collect returns 0 reviews
unexpectedly.** Exit code is 0 only on `ok`/`empty`; 1 on any alarm state.

Five states:

| State | Meaning |
|---|---|
| `ok` | ≥1 review parsed — parser healthy |
| `drift` | Page has review markup (`elv-stars` / `-review-` ids) but the parser extracted 0 — **THE alarm**: selectors are stale |
| `empty` | No review markers at all — genuinely no reviews for this product (new/quiet product) |
| `challenge` | DataDome interstitial — a network/anti-bot problem, not a parser problem |
| `error` | Fetch failed |

Options: `--slug` (default `sierra`), `--headless/--headed` (default: headed, which G2 requires).

### Test inventory


| File | Tests | What it covers |
|---|---|---|
| `test_g2_extract.py` | 16 | `extract_g2_reviews` on live `elv-*` fragments (counts, field mapping, rating scale, verified tokens, NPS/helpful fields) |
| `test_g2_parse.py` | 5 | Parser extracts all fields, handles anonymous reviewers, empty HTML |
| `test_g2_adapter.py` | 16 | plan() requires g2_slug, returns correct URL; parse() returns candidates, filters old reviews |
| `test_g2_harvest.py` | 1 | upsert_g2_reviews persists, idempotent (no duplicates on re-run) |
| `test_g2_export.py` | 2 | JSON export decodes pros/cons to lists; CSV export flattens to semicolon strings |
| `test_g2_slug.py` | 4 | Account.g2_slug field, to_db_row, from_db_row |
| `test_g2_reviews_db.py` | 5 | g2_reviews table exists with all columns (incl. nps_score/helpful_votes + migration); upsert round-trip incl. NPS/helpful |
| `test_cli_g2.py` | 2 | g2-export command exists, creates JSON + CSV files |
| `test_runner_cf_g2.py` | 1 | Cloudflare bypass routes marketplace_g2 tasks |
| `test_runner_g2.py` | 10 | runner `_fetch_g2_fragment` renders the reviews_and_filters fragment for G2 tasks |
| `test_g2_e2e.py` | 1 | Full pipeline: plan -> parse -> harvest -> export -> idempotent upsert |
| `test_capterra_parse.py` | 7 | `extract_capterra_reviews` on the live Capterra fixture — 25-card parity, field mapping, pros/cons, month-name dates, empty-HTML |
| `test_capterra_adapter.py` | 14 | plan() multi-slug fan-out (`<id>/<Slug>` segments), parse/harvest on the fixture, follow_tasks pagination + cap, cookie headers |
| `test_runner_capterra.py` | 4 | normal-fetch routing (no stealth browser), CF challenge → bypass fallback, follow-pass re-entry |
| `test_capterra_db.py` | 5 | `source` column (NEW_COLUMNS migration), `upsert_capterra_reviews` round-trip + idempotency, G2 default source='g2' |
| `test_trustradius_parse.py` | 7 | `extract_trustradius_reviews` on the live TrustRadius fixture — 3-card parity, 0-10→0-5 rating normalization, ISO dates, reviewer/company-size mapping, empty-HTML |
| `test_trustradius_adapter.py` | 13 | plan() multi-slug fan-out, parse/harvest on the fixture, follow_tasks pagination + cap, cookie headers |
| `test_trustradius_db.py` | 3 | `upsert_trustradius_reviews` source='trustradius' round-trip + idempotency, source isolation |
| `test_g2_selfcheck.py` | 14 | per-vendor self-check ok/empty/drift/challenge/error states with mocked fetchers (g2 + capterra + trustradius wrappers) |

### Fixtures

Live `elv-*` fragments (frozen real captures of the `reviews_and_filters` DOM, used by
`extract_g2_reviews`):

| File | Product | Notes |
|---|---|---|
| `g2_reviews_live_sierra.html` | sierra | Real rendered review cards |
| `g2_reviews_live_helcim.html` | helcim | Real rendered review cards |

Legacy `itemprop` page (kept for `parse_g2_reviews` backward-compat coverage),
`g2_reviews.html` — a frozen HTML page with 3 review cards:

| # | Reviewer | Rating | Date | Verified | Source |
|---|---|---|---|---|---|
| 1 | John D (span) | 4.5 (stars-9) | 2026-01-15 | Yes | Organic |
| 2 | Jane S (span) | 5.0 (stars-10) | 2025-12-03 | Yes | Invitation from G2 |
| 3 | Anonymous User (div) | 3.5 (stars-7) | 2025-11-20 | Yes | Organic |

Review 3 uses `<div itemprop="author">` instead of `<span>` to test both code paths.

---

## How It Fits in Signals

Signals is a sales signal enrichment engine that collects buying signals about target accounts
from 40+ zero-cost source keys, resolves them to an identity graph, scores and tiers them, and exports
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
  does not bypass authentication, paywalls, or login-gated content. It is scoped to `techstack`,
  `marketplace_g2`, `marketplace_capterra`, and `marketplace_trustradius` — other sources retain
  the 403 hard-stop.

- **No auth bypass**: There is no automated login. Sign-in for full review text (if needed) is a
  manual, opt-in flow where the user provides a G2 session cookie. No password storage, no
  automated login form submission.

- **Rate limiting**: The adapter uses a 0.5 req/sec rate limit and a 168-hour (weekly) cadence.
  Polite HTTP. A real contact address in the User-Agent.

- **No reselling**: The raw store is a local reproducibility cache. Never resell raw content.

---

## Features

The G2 adapter implements the capabilities below; each maps to a shipped, test-pinned feature (see the test inventory at the bottom of this file).

- **Review-velocity trend signals** — each collect diffs the per-slug review count/avg-rating
  against the prior cycle (`data/marketplace/stats.json`) and emits a `marketplace_review_trend`
  signal when deltas cross the configured thresholds (`review_trend:` in
  `config/marketplace.yaml`). Rating drops and review surges become triggers, not just raw rows.

- **Empty-since bookkeeping with cadence backoff** — `EmptyLog` tracks consecutive empty cycles
  per slug/source (atomic JSON, injectable clock); after 3 consecutive empties the planner skips
  the slug ("empty since" date logged), and any cycle with reviews resets the counter. All five
  marketplace adapters record into it: G2 via its fragment path, the other four via the
  shared harvest-reviews path — so `_filter_backoff`'s empty-since backoff is fully wired for
  every marketplace source, not just G2.

- **Multi-page pagination** — `follow_tasks()` plans the next review page (`?page=N`) up to
  `max_review_pages` (default 5). The runner fetches each page in turn so a single collect run
  sweeps multiple pages per account, not just the first.

- **Show More expansion** — when `deep_reviews: true` is set in `config/marketplace.yaml`, the
  runner requests "Show More" clicks on each review card to expand truncated bodies before
  parsing (browser tier). **Bounded by `deep_reviews_bound`** (default `extreme`: only ratings
  >=4.0 or <=2.0 are expanded — unknown ratings expand conservatively; explicit `null` restores
  legacy bound-free expansion), enforced via `filter_deep_reviews` in `g2.py`.

- **Session cookie support** — set `session_cookie_file` to a JSON cookie file (Playwright/
  browser export format) to attach a G2 sign-in session. This unlocks full review text that G2
  gates behind login without any automated login form submission.

- **BeautifulSoup4 parsers** — `extract_g2_reviews()` (current `elv-*` DOM) and
  `parse_g2_reviews()` (legacy `itemprop` fallback) use `bs4.BeautifulSoup` with the `lxml` parser
  instead of a hand-rolled `HTMLParser`. More resilient to G2's obfuscated, shifting class names
  and nested structure.

- **2Captcha solver wiring** — the Cloudflare bypass waterfall's tier 3 (`_solver_via_browser`)
  calls the 2Captcha/anti-captcha Turnstile API and injects the token via
  `inject_turnstile_token` (browser navigation → `add_cookies` → reload →
  challenge-cleared poll). Both the API call and the token-to-cookie browser
  injection are wired (see Cloudflare Bypass / Limitations).

- **Auto-resolve g2_slug** — `python -m src.cli resolve --g2` resolves a product slug from the
  company name via G2 search (`src/identity/g2_resolve.py`) and writes it back to the account,
  so accounts can be seeded without manually looking up slugs.

- **Config-driven lookback** — `review_lookback_days` (default 90) in
  `config/marketplace.yaml` controls how old a review can be and still emit a signal. No longer
  hardcoded.

- **Stealth-browser lifecycle management** — the runner owns the stealth (Patchright) browser
  and closes it in `run()`'s `finally` (success or exception). No leaked headed Chromium
  processes between collection runs.

- **Warm-up gating + fragment pagination** — the behavioral warm-up runs once per session
  (page 1); pagination pages 2..N (`follow_tasks` → `?page=N` fragment fetches) skip it. The
  two-pass flow (page 1 parse → plan page 2 → fragment refetch) is contract-tested.

- **Fragment state classification** — every fragment fetch is classified `ok` / `empty` /
  `challenge` (attached as `g2_state` on the Document) and logged: empty slugs (new products
  with no reviews) are INFO and distinct from blocks; DataDome challenges emit a WARNING with
  the fix ("re-export data/g2_cookies.json") and fall back to the bypass waterfall.

- **Cookie-lifetime learning** — fragment outcomes feed the anti-bot `RouteState`
  (`record_solve` on success, `expire` on challenge) so per-domain cookie lifetimes converge
  from real observations, and the warm-up is skipped automatically once cookies are known-stale.

- **Multi-slug accounts** — `g2_slug` accepts comma-separated slugs; one collection task per
  G2 product.

- **NPS score + helpful votes extracted where present on review cards (None otherwise)** —
  `G2Review` / `g2_reviews` carry `nps_score` and `helpful_votes`. Step 0 discovery (Aug 2026,
  live sierra/helcim/harmonic captures) found no identifiable per-card markup: "nps_score"
  appears only in the star-filter widget, and the leading card metas are constant across all
  cards, so no selector was invented — extraction returns `None` until real markup maps.

---

## Limitations & Roadmap

### Current limitations

- **DataDome blocks headless mode**: The DataDome bypass (tier 2.5 Patchright) only succeeds in
  HEADED (visible) mode. In headless, DataDome's Proof-of-Browser fingerprint detects the software
  SwiftShader WebGL renderer and serves the `rt='i'` interstitial even with Patchright's CDP/flag
  patches. Additionally, DataDome scores sessions and can challenge a clean headed browser
  intermittently — success is not deterministic. Set `browser.headless: false` for G2 collection.

- **G2 DOM drift**: G2 obfuscates class names and restructures pages over time. The parser is
  tested against a frozen fixture; live G2 markup may diverge and require selector maintenance.
  BeautifulSoup4 softens this (CSS selectors, tolerant tree walking) but cannot eliminate it.

- **Cloudflare Turnstile escalation**: G2 may escalate to managed/Turnstile challenges that the
  browser-only JS solve (tier 2) cannot clear. The 2Captcha solver tier (tier 3) now injects the
  token as a `cf_clearance` cookie via the browser and polls for clearance (mock-tested; live
  validation with a real 2Captcha key is a manual step). Headed fallback (tier 4) remains
  available if injection doesn't clear.

- **Marketplace trend/empty state is per-process+JSON**: review-velocity stats
  (`data/marketplace/stats.json`) and empty-since bookkeeping (`EmptyLog`) are JSON files with
  atomic writes but no cross-process lock — run a single collect process at a time (the
  scheduler's `SingleFlight` lock enforces this for watch-mode runs).

- **G2 ToS**: G2's Terms of Service restrict automated scraping. The adapter stays disabled by
  default and opt-in. Enable at your own risk (see Legal & Ethics).

### Roadmap

- **Extra review fields** — `nps_score` and `helpful_votes` columns/fields now exist but
  extraction yields `None` until identifiable per-card markup is captured (see Features);
  reviewer metadata beyond what `extract_g2_reviews` maps remains open.
- ~~**Multi-slug accounts**~~ — **Done.** `g2_slug` accepts comma-separated slugs; `plan()`
  fans out one FetchTask per G2 product (see Features).
- ~~**Selector-drift self-check**~~ — **Done.** `g2-selfcheck --slug <slug>` fetches one known-good
  product headed and asserts ≥1 review parses; exits 1 with `state=drift` when live markup
  diverges from what the parser selects (see Testing → Self-check).
- ~~**Wire the 2Captcha token-to-cookie browser injection**~~ — **Done.** Tier 3 now injects the
  token as a `cf_clearance` cookie via the browser (`inject_turnstile_token`: navigate →
  add_cookies → reload → challenge-cleared poll) and polls for clearance (see Limitations).
- ~~**"Empty since" bookkeeping**~~ — **Done.** `EmptyLog` (`src/sources/marketplace/empty_log.py`)
  persists empty-slug checks; the runner backs off products with 3+ consecutive empty cycles
  and records empties per cycle (see runner.py record_empty).

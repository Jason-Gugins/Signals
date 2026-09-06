# Waterfall Enrichment Playbook

Given a domain or company name, this playbook lists every enrichment flow the
Signals engine can run — and the combinations that turn raw signals into
outreach angles. Every command here exists in the CLI (verified against
`src/cli.py`) and every signal type named here exists in
`config/signals.yaml`.

> Part of [Signals](README.md). The LinkedIn scraper is the private companion
> repo (`linkedin-scraper`) — see `src/sources/linkedin_db/README.md` for the
> integration contract.

## 0. The engine in one page

- An **Account** is a company row (domain = join key) in `data/signals.db`
  with optional enrichment fields: `linkedin_slug`, `cik`, `g2_slug`,
  `ats_token`, `blog_feed_url`, `app_store_id`, `extra_data.bbb_url`.
- A **Source** declares `requires` (account fields it needs), `emits` (signal
  types), and a cadence. `collect` runs every enabled source whose
  prerequisites the account satisfies — sweep reports the rest as skipped.
- **Classify/score/tier** turn raw candidates into ranked signals;
  **plays/digests** deliver them per account.

Onboarding — one command, seed-or-update + run everything + report gaps:

```powershell
.\.venv\Scripts\python.exe -m src.cli sweep https://acme.com
```

## 1. Source inventory

| Source key | Needs | Emits | Cadence | Notes |
|---|---|---|---|---|
| `google_news` | — | funding_round, exec_hire, exec_departure, product_launch, ma_*, ipo_*, layoff, earnings_warning, office_open, award, certification, market_consolidation, competitor_outage | 12h | reranked (floor 0.35); keyword SERPs |
| `news_rss` | — | same classifier (name query + Bing News) | 12h | |
| `company_feed` | `blog_feed_url` | product_launch + blog-derived signals | 24h | the company's own blog |
| `sec_edgar` | `cik` | ipo_filing, ipo_pricing, ma_*, annual_report_10k, exec_*, layoff, earnings_warning, bankruptcy_signal, contract_terminated, insider_trade | 24h | private cos = empty by design (no CIK); 8-K items 1.03 (bankruptcy, chapter captured) and 1.02 (agreement terminated) mapped; Form 4 XML fanout (10 docs/cycle cap, net bought/sold); SC 13D/G stakes → ma_target (amendments unwatched) |
| `sec_formd` | — | funding_form_d | 24h | global Form D fanout |
| `federal_contracts` | — (matches on account name) | federal_contract_award | 168h | live usaspending.gov award search (keyless POST, trailing 12 months); never-guess recipient matching — ambiguous names emit nothing |
| `marketplace_g2` | `g2_slug` | review/sentiment + reviewer signals | 168h | ships disabled; ≤2 req/host, ≥4s pacing |
| `marketplace_capterra` | `g2_slug` | same shape as G2 | 168h | ships disabled; needs numeric-id slug |
| `marketplace_trustradius` | `g2_slug` | same shape | 168h | ships disabled |
| `ats_greenhouse` / `ats_lever` / `ats_ashby` / `ats_smartrecruiters` / `ats_breezy` / `ats_jobvite` / `ats_recruitee` / `ats_teamtailor` / `ats_rippling` / `ats_workday` / `ats_workable` | `ats_token` | job postings | 12h | vendor board APIs |
| `ats_careers_page` | — | job postings (careers-page scrape) | 24h | gated OFF when a vendor ATS matches |
| `jobsignals` | job postings in DB | hiring_surge, leadership_job_open, department_expansion, new_geo, backfill_open, tech_migration_mentioned, internal_project_scoop | derived | job-text extraction: required-stack phrasing, case-insensitive vendor allowlist, all matches per job |
| `linkedin_db` | `linkedin_slug` | exec_hire, champion_migration, leadership_job_open + contacts | 6h | DB-only; live deepen is manual |
| `warn_notices` | — | WARN layoff notices | 24h | NY+CA fan out in code; NJ/FL/OH/WA/TX/IL parsers present; MI deferred |
| `techstack` | — | tech_install_new, tech_churn, tech_removed, renewal_window, high_ticket_tech, competitor_detected, competitor_outage | 168h | HTML + HAR-lite + DNS-probe evidence; `renewal_window` from stored first-seen anniversaries; `competitor_detected` fires for vendors in the fingerprints `competitors:` list; polls `*.statuspage.io/index.json` when `status.<domain>` CNAMEs there |
| `wayback` | — | pricing_change, positioning_change | — | archived homepage title/meta-description diff + `/pricing` diff |
| `yc_batch` | — | YC-company context | — | disabled by default (stub) |
| `repvue_db` | — | sales-hiring context | — | optional local DB |
| `appstore_reviews` | `app_store_id` | app-review sentiment + review-velocity trend | 168h | trend wiring fixed (was dead: missing slug + garbled source key); captures app version per review |
| `bbb_profile` | `extra_data.bbb_url` | identity evidence, intent signals, reputation_drop | 720h | rating downgrade / accreditation loss vs the stored baseline (first observation = baseline only) |
| `owned_intent` | `data/inbox/owned/*.csv` | first-party intent | 1h | local files |

Marketplace and yc sources ship `enabled: false` in `config/sources.yaml` —
opt in per account when the slug/prereq exists.

## 2. The flows

Each flow: prerequisites → commands (copy-pasteable from repo root) → what
you get → caveats.

### Flow A — Quick triage (5 minutes, any domain)

Prereqs: none.

```powershell
.\.venv\Scripts\python.exe -m src.cli sweep https://acme.com
.\.venv\Scripts\python.exe -m src.cli brief --domain acme.com
```

What you get: account created/updated, the identity resolver pass (CIK, ATS,
feeds, ICP) run for new accounts, every eligible source run (google_news +
news_rss + warn + techstack + wayback immediately), a digest of signals, and a
list of sources skipped with the exact missing field.

Caveats: bare company names are refused (v1) — pass a URL or domain. The
opt-in resolvers (`--appstore`, `--bbb`, `--linkedin`) are not part of sweep;
run them via `resolve` when you want them.

### Flow B — Leadership tracking (LinkedIn)

Prereqs: companion scraper checkout (`../Linkedin`) with its venv; a fresh
session (manual `login` in the scraper); account seeded with `linkedin_slug`.

```powershell
.\.venv\Scripts\python.exe -m src.cli deepen --domain acme.com
# the 6h harvest converts scraper-DB rows into signals automatically, or force it:
.\.venv\Scripts\python.exe -m src.cli collect --source linkedin_db --domain acme.com
```

What you get: `exec_hire` (current role started ≤6 months, leadership-titled),
`champion_migration` (a known champion landed at this company),
`leadership_job_open` from scraped jobs, plus people→contacts.

Caveats: **manual-only** — the scraper enforces ≤5 companies/run; the first
LinkedIn account used was restricted after an aggressive run, so keep runs
sparse; sessions expire in weeks (`status` in the scraper warns at 30 days);
`extract` needs the exact LinkedIn slug (`databricks`, not `databricks.com`);
`exec_hire` needs profile scrapes — listing rows carry no role history.

### Flow C — Hiring tracks (ATS / careers page)

Prereqs: for vendor ATS — the board token on the account (`ats_token`); for
the careers-page path — nothing (URL scrape).

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source ats_careers_page --domain acme.com
# vendor path once ats_token is seeded:
.\.venv\Scripts\python.exe -m src.cli collect --source ats_greenhouse --domain acme.com
```

What you get: job postings that `jobsignals` turns into `hiring_surge`,
`leadership_job_open`, `department_expansion`, `new_geo`.

Caveats: `ats_careers_page` is mutually exclusive with a vendor match (no
duplicate rows); vendor cadence is 12h; careers-page scraping depends on the
site's markup.

### Flow D — Funding tracks (News + SEC EDGAR)

Prereqs: none for news; a CIK for EDGAR.

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source google_news --domain acme.com
.\.venv\Scripts\python.exe -m src.cli collect --source sec_edgar --domain acme.com --force
```

What you get: `funding_round` (amount + stage extracted, reranked against
false positives), `funding_form_d`, `ipo_filing`/`ipo_pricing`, M&A.

Caveats: private companies have no CIK — EDGAR returns empty *by design*;
confirmed-empty is still information (and an S-1 will fire the moment one is
filed). News attribution is guarded by the publisher-domain ladder (a false
"Dolce Glow raises $11M" for account Glow is rejected at tier-2).

### Flow E — Product sentiment (G2 / Capterra / TrustRadius)

Prereqs: the marketplace slug on the account (`g2_slug`); sources enabled in
`config/sources.yaml` (they ship disabled).

```powershell
.\.venv\Scripts\python.exe -m src.cli resolve --g2
.\.venv\Scripts\python.exe -m src.cli collect --source marketplace_g2 --domain acme.com
```

What you get: product sentiment, reviewer-ICP shape (who actually reviews
them), rating trends.

Caveats: G2 is anti-bot hard (≤2 req/host, ≥4s pacing, DataDome); Capterra
slugs are `<numeric-id>/<Slug>` segments; scripted Capterra access is
IP-burned — real-browser capture is the fallback.

### Flow F — Layoff / distress (WARN + news)

Prereqs: none.

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source warn_notices --domain acme.com
```

What you get: `layoff` signals from WARN filings (NY+CA fan out in code;
NJ/FL/OH/WA/TX/IL parsers present) plus news-rule layoffs.

Caveats: WARN covers regulated layoff sizes only; dates are filing dates.

### Flow G — Full waterfall

Prereqs: fill in what sweep tells you is missing.

```powershell
.\.venv\Scripts\python.exe -m src.cli sweep https://acme.com
# new accounts get CIK/ATS/feeds/ICP resolution automatically;
# opt-in resolvers fill app_store_id / bbb_url / linkedin_slug for the cohort:
.\.venv\Scripts\python.exe -m src.cli resolve --appstore --bbb --linkedin
.\.venv\Scripts\python.exe -m src.cli sweep https://acme.com --deep
.\.venv\Scripts\python.exe -m src.cli digest --period weekly --domain acme.com
```

What you get: every source the account qualifies for, one digest — plus a
"New Form D issuers (unmatched)" section on non-domain digests listing
newly-funded companies the registry doesn't know yet.

## 3. Combination recipes

| Signals | Sales angle | Verify first |
|---|---|---|
| `exec_hire` × `champion_migration` | Your champion just switched companies — poach/expand at the new desk | person is actually seeded as a champion |
| `leadership_job_open` × `techstack` | They're hiring a leader for a function your tool replaces | the techstack hit is in the same function |
| `funding_round` × `hiring_surge` | Fresh money + scaling team = active buying window | funding date within ~90 days |
| marketplace sentiment decline × `exec_departure` | Turnaround chaos — competitor displacement or rescue pitch | sentiment delta is real, not review-volume noise |
| `new_geo` (`department_expansion`) × `warn_notices` in the old region | Team relocating — new stack decisions at the new site | the WARN company maps to the same account |

## 4. Prerequisites checklist

| Field | How to get it |
|---|---|
| `g2_slug` | `python -m src.cli resolve --g2` (marketplace search; scope via the global `--cohort` flag; candidates-only on ambiguity — a wrong guess is never auto-persisted) |
| `ats_token` | Detect the ATS from the careers page; the vendor board token is then set on the account |
| `cik` | Auto at sweep/`resolve --cik` (SEC tickers match); manual: EDGAR full-text search (efts.sec.gov) |
| `linkedin_slug` | `python -m src.cli resolve --linkedin` (companion-scraper discover subprocess; human-triggered) or the funded-companies list (`src/sources/funded_software_companies.md`); must be the exact LinkedIn slug |
| `blog_feed_url` | Auto-discovered at seed/sweep time (`src/identity/feed_discovery.py` — falls back to archived homepage snapshots when the live site is a JS shell) |
| `app_store_id` | `python -m src.cli resolve --appstore` (iTunes Search API; candidates-only on ambiguity) |
| `extra_data.bbb_url` | `python -m src.cli resolve --bbb` (BBB search API; candidates-only on ambiguity) |

## 5. Operational caveats (read once, saves pain)

- **LinkedIn is manual-only by design.** The scraper enforces ≤5 companies per
  run; the first account used was restricted by LinkedIn after an aggressive
  run. Sparse, human-triggered `deepen` only; no automated login, ever.
- **G2 is anti-bot hard**: ≤2 requests/host, ≥4s pacing; challenges abort the
  run. Capterra scripted access is IP-burned — use real-browser capture.
- **The reranker ships enabled** (ms-marco-MiniLM, floor 0.35) — install the
  extra first: `pip install -e ".[rerank]"`. Without it, scoring silently
  degrades to keep-all (log line tells you).
- **EDGAR empty ≠ broken.** Private companies have no CIK; empty results on a
  private account are expected and still meaningful.
- **Cadences matter**: `collect` without `--force` skips sources that ran
  within cadence; sweep force-runs only on first creation.

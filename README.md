# Signals

Sales signal enrichment engine. Continuously collects buying signals about
target accounts from ~20 self-built, **zero-cost** sources, resolves them to a
single account identity graph, scores and tiers them per High Probability
Prospecting (HPP), maps each signal to a sales play, and exports ranked
account briefs.

No paid APIs. No ZoomInfo, Apollo, Exa, BuiltWith, or Bombora.

Public, business-relevant data only. Polite HTTP (`robots.txt` honored by
default). A real contact address in the User-Agent. Rate limits are floors.
Marketplace scraping is opt-in — see the *Sources* section below for the full
gate and per-site options.

## Pipeline

`seed` → `resolve` → `collect` → `score` → `brief` → `export`

## Clone and install

Windows 10. Python 3.12.

```powershell
git clone https://github.com/Jason-Gugins/Signals.git
cd Signals
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m patchright install chromium
copy .env.example .env
# set SIGNALS_CONTACT_EMAIL to a real address (SEC EDGAR 403s without it)
```

## Verify

```powershell
.\.venv\Scripts\python.exe -m src.cli doctor --no-network
.\.venv\Scripts\python.exe -m pytest -q
```

`pip install -e .` also installs a `signals` console script — a shorthand for
the `python -m src.cli` invocations shown throughout this README.

CI runs the same suite on every push/PR (Ubuntu + Windows matrix, offline
lane) plus a nightly live-parity lane — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Init and file drops

```powershell
.\.venv\Scripts\python.exe -m src.cli init
```

`config/lists/` is gitignored — `init` creates the directory, but you must
populate it (a human provides):

- `config/lists/champions.csv` — prior buyers (unlocks `champion_migration`;
  load with `.\\.venv\\Scripts\\python.exe -m src.cli champions --load config/lists/champions.csv`)
- `config/lists/exclusions.txt` — domains to skip
- `config/lists/email_patterns.csv` — only if a pattern is *known*
- `data/inbox/owned/*.csv|*.jsonl` — first-party intent (web/ESP export)

## Everyday commands

`--dry-run` goes **before** the subcommand (`python -m src.cli --dry-run collect`).

```powershell
.\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv --linkedin --repvue
.\.venv\Scripts\python.exe -m src.cli run --cohort canada-software
.\.venv\Scripts\python.exe -m src.cli brief --domain acme.com
.\.venv\Scripts\python.exe -m src.cli export --format csv
.\.venv\Scripts\python.exe -m src.cli watch --once
```

Also: `init`, `resolve` (`--g2`, `--capterra` — slug discovery via each
marketplace's search), `collect`, `reparse`, `score`, `status`, `doctor`,
`accounts`, `champions`, `signals`, `deepen`, `g2-export`, `g2-selfcheck`,
`capterra-selfcheck`, `prune` (retention: delete old fetch_log/documents/runs
rows + raw files past `--keep-days`, then WAL checkpoint + ANALYZE; `--vacuum`
reclaims space), `plays --outcome hit|miss --domain X --play Y` (record a
local play outcome for backtesting), `plays-report` (per-play sent/hit/rate
table), `plays-calibrate` (feed decided outcomes into the calibration table),
`digest --period daily|weekly [--domain X] [--email]` (per-account markdown
alert digest in `data/digests/`; `--email` sends it via SMTP), and
`selfcheck --source techstack|news_rss|ats_greenhouse` (generic five-state
selector-drift check; challenge/empty exit 0, drift/error exit 1).

Outputs land in `data/exports/`, `data/briefs/`, `data/digests/`, and
`data/alerts/`.
Raw bytes live in `data/raw/<xx>/<sha>.gz` (content-addressed gzip).

## Email delivery

Briefs and digests can be emailed via stdlib SMTP (`digest --email`,
`brief --email`). Configure in `.env`:

```
SIGNALS_SMTP_HOST=smtp.example.com
SIGNALS_SMTP_PORT=587
SIGNALS_SMTP_USER_ENV=SIGNALS_SMTP_USER   # env var NAMES, not credentials
SIGNALS_SMTP_PASS_ENV=SIGNALS_SMTP_PASS
SIGNALS_EMAIL_TO=you@example.com
```

The actual username/password live in the env vars those names point at and are
resolved at send time — never stored in config. Send failures are logged and
never crash the run (the digest/brief file is written either way).

## Form D funding

SEC private-offering notices. Pooled investment funds are excluded by default.
Unknown issuers land on stub domains like `cik0001234567.edgar`.

`funding company --domain radicl.com` fetches the homepage, peels a legal name,
quoted Form D search, and drops name collisions (Scanner ≠ Surgical Safety Scanner).

```powershell
.\.venv\Scripts\python.exe -m src.cli funding recent --days 30 --limit 100
.\.venv\Scripts\python.exe -m src.cli funding search "robotics" --days 365
.\.venv\Scripts\python.exe -m src.cli funding company --cik 0001234567
.\.venv\Scripts\python.exe -m src.cli funding company --domain radicl.com
.\.venv\Scripts\python.exe -m src.cli collect --source sec_formd --force
.\.venv\Scripts\python.exe -m src.cli export --what funding
```

## Sources

Enabled adapters live in `config/sources.yaml`:

- SEC: `sec_edgar`, `sec_formd`
- ATS: `ats_greenhouse`, `ats_lever`, `ats_ashby`, `ats_smartrecruiters`, `ats_workable`, `ats_recruitee`, `ats_workday`
- ATS (P2): `ats_rippling`, `ats_jobvite`, `ats_breezy`, `ats_teamtailor` (same `ats_vendor`+`ats_token` contract), plus `ats_careers_page` — a conservative `/careers` HTML fallback that fires ONLY for accounts with no ATS vendor/token

**Making an ATS source fire:** set the account's `ats_vendor` and `ats_token`
(Feed Me CSV columns at seed time, or `deepen` for per-account updates);
`ats_careers_page` then stays quiet for that account (no duplicate job rows)
and covers accounts with neither field set.

- News / regulators: `news_rss`, `google_news`, `company_feed`, `federal_register`, `warn_notices`

`google_news` fetches keyword search RSS (per-account name) plus named section feeds (TECHNOLOGY, BUSINESS). Same `classify_news` pipeline — funding, exec hires, M&A, product launches. Configurable topics in `config/sources.yaml`. SERP manipulation: the account query is also run augmented with signal keywords (fundraising, new leadership, new GTM product, acquisition) for higher recall — see `serp_keywords` in `config/sources.yaml`.

- Footprint: `techstack`, `wayback`, `crtsh`, `jobsignals`
- Community: `community_hn`, `community_github`
- App stores / registry: `appstore_reviews` (live iTunes RSS reviews; set the
  account's `app_store_id` — a seed CSV column or `deepen` update),
  `bbb_profile` (live BBB business profiles; seed
  `extra_data.bbb_url` per account — URLs are not derivable from name+domain)
- Local / opt-in DBs: `owned_intent`, `linkedin_db`, `repvue_db`, `content_itunes`

Disabled by default: `community_reddit` (live fetching blocked — www 403 block
page + old.reddit login wall, P2 spike 2026-08-31), `yc_batch` (Y Combinator
directory is a client-rendered Inertia shell; parser needs a browser-tier
upgrade), `content_producthunt` (Cloudflare-blocked). Marketplace collection
(`marketplace_g2`, `marketplace_capterra`, `marketplace_trustradius`) is
opt-in — the adapter's `enabled` flag in `config/sources.yaml` is the enforced
gate and all three ship `false`. The `sites.*` blocks in
`config/marketplace.yaml` hold per-site options (cookie file, page limits) —
setting a site's `enabled` there is documentation of intent, not an additional
code-enforced gate.
G2 is a browser-tier DataDome target (live runs need
`DATADOME_SOLVER_PROVIDER` / `DATADOME_SOLVER_API_KEY` /
`DATADOME_RESIDENTIAL_PROXY` — see `.env.example`); Capterra and TrustRadius
are server-rendered HTTP behind Cloudflare. ToS restricts automation — full
notes for all three in `src/sources/marketplace/README.md`.

`techstack` indexes observed third-party hosts from HTML/HAR-lite; YAML only *names* common platforms (HubSpot, Webflow, GTM, …). Unknown SaaS still lands as `host:cdn.example` in `technologies`, not as `tech_install_new`. After seed: `.\.venv\Scripts\python.exe -m src.cli collect --source techstack --force`. Full notes: [`src/sources/techstack/README.md`](src/sources/techstack/README.md).

### Cloudflare bypass

When `techstack` hits a Cloudflare challenge (403 or managed interstitial), a 5-tier bypass waterfall attempts to solve it: cached `cf_clearance` cookie reuse → headless Chromium JS solve → external solver (2Captcha Turnstile token, re-injected via browser as a `cf_clearance` cookie and polled for clearance) → headed manual fallback → honest hard stop (names `cloudflare`, invents nothing). Bypass is scoped to `techstack`, `marketplace_g2`, `marketplace_capterra`, and `marketplace_trustradius` (`_CF_BYPASS_SOURCES` in `src/pipeline/runner.py`); all other sources retain the 403 hard-stop. Cookies persist in `cloudflare_cookies` (UA + proxy bound). Enable the solver in `.env`:

```
CLOUDFLARE_SOLVER_PROVIDER=2captcha
CLOUDFLARE_SOLVER_API_KEY=your_key
CLOUDFLARE_BYPASS_STRATEGY=browser_first
```

See [`src/sources/techstack/README.md`](src/sources/techstack/README.md) for the full waterfall and [`src/sources/techstack/cf_bypass.py`](src/sources/techstack/cf_bypass.py) for the implementation.

## Signals and the backtesting loop

P2 added signal families beyond news: `pricing_change` (wayback snapshots of
`/pricing` diffed each cycle — a plan/price change means budget is moving) and
`hiring_surge` (open-role deltas from the ATS/job-board sources, with both a
minimum percentage delta and an absolute-count floor so tiny boards don't
fire). Entity resolution (`normalize_entity` + fuzzy match, `config`
`entity_aliases`) ties same-company variants across sources to one account,
and `config/icp.yaml` scores accounts at seed time so tiering is real from
day one.

### Relevance reranking (optional)

Google News matches on company name are surface-level: an `"Acme" CEO` SERP
query returns tangential coverage that keyword guards can't fully judge. The
optional cross-encoder reranker scores every SERP candidate for true semantic
relevance, drops below-floor matches before classification, and stamps
survivors with `evidence_data["relevance"]` (which then decides which same-day
candidates survive the per-type cap). It's **off by default** and fully
optional — the whole pipeline runs unchanged without it:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rerank]"
# then in config/default.yaml:  rerank: { enabled: true, floor: 0.35 }
```

Implementation: `ms-marco-MiniLM-L-6-v2` cross-encoder (22M params, Apache-2.0)
via ONNX Runtime on CPU (~1-4ms/pair; a full SERP pool costs ~0.1-0.3s per
account), model revision pinned in `src/signals/rerank.py`, session memoized
per process. If the model can't download or inference fails, that feed falls
back to unranked with a logged warning — reranking is an enhancement, never a
dependency.

The play-outcome loop closes the calibration circuit:

```powershell
# after runs, record what happened to the plays you worked:
.\.venv\Scripts\python.exe -m src.cli plays --outcome hit --domain acme.com --play growth_pitch
.\.venv\Scripts\python.exe -m src.cli plays-report       # per-play sent/hit/rate
.\.venv\Scripts\python.exe -m src.cli plays-calibrate    # feed decided outcomes into calibration
```

Once ≥30 decided outcomes exist for a (source, signal_type), `blend_confidence`
starts adjusting candidate confidence from real hit rates. Digests are the
read-side: `digest --period daily` writes per-account markdown (grouped by
signal type with why-now lines) to `data/digests/`; add `--email` to send it.

An account health score (polarity of its signals — a WARN + layoffs account
scores negative) gates growth-family plays automatically, so you don't pitch
growth into a layoff. Defaults are fine; tune via `config/health.yaml` and the
`health_gate` block in `config/plays.yaml` (add the key to override the
code defaults: threshold −0.5, suppressed family `growth_pitch`).

## Anti-Bot Evasion

`src/antibot` (SignalsShadow) is a native Rust/BoringSSL fetch tier whose TLS
fingerprint is emergent from Chrome's own engine — "Chrome TLS, not
Chrome-like" — plus an in-house HTTP/2 stack (Akamai h2 byte-exact), temporal
stealth (session resumption, pooling, 304 revalidation), and solve-and-bounce
ghost orchestration with per-domain routing that learns clearance-cookie
lifetimes (RouteState, `data/antibot/routing.json`). Falls back to
`curl_cffi` when the engine isn't built. No CAPTCHA solving, no login
bypass — honest limits are documented. Full notes: [`src/antibot/README.md`](src/antibot/README.md).

## Scheduling, retention, and calibration (ops)

- **Durable scheduling** — `watch` runs on a `Scheduler` (`src/pipeline/scheduler.py`):
  per-source cadence from `config/sources.yaml`, missed-run catchup, ±5%
  jitter, failure backoff (cadence ×2 per consecutive failure, capped 8×), and
  a single-flight lockfile (`data/state/collect.lock`, atomic create, stale
  lock auto-break). OS cron invoking `collect` per source remains the
  recommended production setup.
- **Retention** — `prune --keep-days N` deletes expired `fetch_log` /
  `documents` / `runs` rows and raw-store files, then WAL-checkpoints and
  ANALYZEs. Add `--vacuum` to reclaim disk.
- **Confidence calibration** — a `calibration` table (schema migration v2)
  stores per-source/per-signal-type hit rates; `blend_confidence` adjusts
  candidate confidence once ≥30 samples exist. The loop is now closed: record
  play outcomes with `plays --outcome`, then `plays-calibrate` feeds decided
  outcomes into the table (P2 backtesting, migration v5).
- **DB migrations** — schema changes run as ordered, transactional migrations
  tracked by `PRAGMA user_version` (currently v6), with a startup
  `integrity_check`.
- **Account health gate** — a pure polarity score (`src/signals/health.py`,
  weights in `config/health.yaml`) suppresses growth-family plays for
  negative-signal accounts (default threshold −0.5; `health_gate` in
  `config/plays.yaml`).
- **Signed webhooks** — beyond the Slack channel, `ALERT_WEBHOOKS_JSON`
  configures generic JSON webhooks with optional HMAC `X-Signature` signing
  (secret referenced by env-var name, resolved at send time).

## Add a source in 20 lines

1. Write a pure `parse_*(body) -> list[SignalCandidate]` (no I/O, no clock).
2. `@register` a `SourceAdapter` with `plan` + `parse`.
3. Import the module in `src/sources/__init__.py` — `@register` only populates
   `SOURCES` if the module is imported there (missing this = silent no-op).
4. Enable it in `config/sources.yaml`.
5. Drop a fixture under `tests/fixtures/<source>/` and a test.

Marketplace-tier sources (browser/HTTP fetcher wiring, a
`config/marketplace.yaml` site entry, a selector-drift selfcheck) are
substantially more involved — see `src/sources/marketplace/README.md`.

## Legal / ethics

- Respect `robots.txt` unless you deliberately turn it off for a run.
- Identify yourself. Set `SIGNALS_CONTACT_EMAIL` — SEC 403s a missing contact.
- No auth bypass, no paywall circumvention, no personal non-work data.
- Cloudflare bot-challenge bypass is scoped to `techstack`, `marketplace_g2`,
  `marketplace_capterra`, and `marketplace_trustradius` (public
  business/review pages). It solves JS/managed challenges to read that
  content — it does not bypass authentication, paywalls, or login-gated
  content.
- G2 / Capterra / TrustRadius / LinkedIn ToS restrict automation — those
  adapters stay disabled by default (opt-in via the `enabled` gate above).
- Never resell raw content. The raw store is a local reproducibility cache.

## First-run checklist

- [ ] What do you sell? (ICP, competitors, play `{your_product}`)
- [ ] Which analytics/ESP? (owned-intent column mapping)
- [ ] Geography focus? (WARN jurisdictions, regulators)
- [ ] Champions list?
- [ ] Alert destination? Slack webhook (`ALERT_WEBHOOK_URL`), signed JSON
  webhooks (`ALERT_WEBHOOKS_JSON`), email (`SIGNALS_SMTP_*`), or file-only.

Deferred: Google Trends, ASN IP→org, CRM write-back, multi-user server.

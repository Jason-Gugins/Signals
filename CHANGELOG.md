# Changelog

## Unreleased

### X-ray SERP prospecting (`xray`)
- New manual, opt-in CLI command `xray`: search-operator prospecting over DuckDuckGo's LITE SERP (`site:`, quoted phrases, `OR`, minus, `intitle:`/`inurl:`) with a string library at `config/lists/xray_strings.yaml` (five families: people-by-title, people-by-niche, hiring-post, recently-funded, intent-problem-phrase). Slots (`--title/--location/--niche/--role/--problem-phrase`) fill the library strings; a string with an unfilled slot is never fetched.
- Engine selection behind `--engine` (default from new config key `xray.default_engine`, itself defaulting to `ddg_lite`), per the 2026-10 Google bypass-ladder probe verdicts in `data/probe/XRAY_SERP_2026_10.md`: the headed-browser and fresh-cookie-replay captures carry a FULL organic result set as embedded `window.W_jd` "2003" url+title state (the prior "zero anchors" verdict was a measurement artifact — result hrefs are opaque `/goto?url=` blobs), so `google_state` parses that state; cookies do NOT amortize ≥1 day (429 captcha on a 24h-old jar) so the jar is refreshed per session via `scripts/google_cookie_refresh.py` (solve → harvest `data/xray/google_cookies.json` → curl_cffi replay) and a missing/empty jar is a clean CLI error; intermittent IP-level captcha walls surface as explicit ledger `challenge` rows, never results; bing/brave/mojeek probed NO-GO (bing silently drops every operator, brave/mojeek hard-walled) and SearXNG is blocked on Docker — `ddg_lite` stays the default.
- `ddg_lite` remains the only keyless path, per the live probe record `data/probe/XRAY_SERP_2026_10.md`: google served a 200 JS-gate shell to every keyless transport, and the html.duckduckgo.com endpoint 403s/challenges operator queries — `lite.duckduckgo.com/lite/` via the chrome-TLS curl_cffi tier is the only probe-validated keyless path, and it is stochastic (202 anomaly / 403 error-lite on some requests), so the runner retries per query (`--attempts`, `--attempt-pause`) with pacing (`--pace`) and a per-run query budget (`--limit`).
- Never-guess persistence: company/hiring/intent queries queue ONE `identity_candidates` row per query (kind=domain, source=xray, the verbatim query string as the review key) with known cohort roots dropped; people-query profile hits become `contacts` rows only on an exact casefold company match against the cohort, unmatched profiles are ledger-only. No new signal types.
- Every query lands in `data/xray/ledger.jsonl` (UTC, status ok/challenge/empty/error, engine, attempts and hit counts); `xray --stats` aggregates per string id so strings that produce can be run more.
- Robots exception #2 (documented in the ethics paragraph and the new README section): manual invocation only, never scheduled, never a cadence fanout, self-paced — the google_state engine adds browser-minutes (cookie refresh) plus a handful of paced replay fetches per session.

### Master intelligence command (`intel`)
- New CLI command `intel <target>` (also `python -m src.cli intel`): one account, every capability, one dossier. The five stages run in order — `identity` → `collect` → `derive` → `score` → `package` — with `--skip STAGE` (repeatable) to drop any of them and `--force` to ignore source cadences.
- The package stage writes a portable package directory under the configured dossiers dir (`data/dossiers/` by default) containing `manifest.json`, `dossier.json`, `dossier.md`, `evidence.jsonl` and `prompt.md`; every claim cites an evidence record. `--no-write` builds the dossier without writing it, and `--max-signals N` caps the active signals.
- Dry-run semantics: planning only, and it requires an account that already exists — an unknown domain is refused with `intel refused: ...` on stderr and a non-zero exit.
- Opt-in posture: LinkedIn slug resolution (`--with-linkedin-resolve`, human-triggered) and the marketplace adapters (`--with-marketplaces`, anti-bot paced) are off by default and surface as coverage gaps when not requested; the single ATS/careers discovery ladder is capped at 10 requests. Seller-market relevance (`needs` / `required_stack_demand`) comes from the seller-market profile in `config/markets.yaml`, which ships empty — nothing is promoted until it is filled in.
- Documented in README (command reference and the master-flow section) and ENRICHMENT.md (Flow H, with `sweep` kept as the lighter onboarding path).

### Profile-matched needs source (`needs`)
- New local-tier source `needs`: it promotes only the observations that match the selected seller-market profile, and cites the evidence verbatim.
- Part 1 — `need_statement`: first-party `company_feed` sentences that say the company is building / rolling out / standing up / migrating / consolidating / expanding / hiring, matched against the profile's served-problem phrases; the quote, doc id, url and offering are carried as evidence.
- Part 2 — `required_stack_demand`: open job postings that explicitly require a vendor (the jobsignals required-stack extractor) whose vendor is in the profile's served set; the job key, url, verbatim phrase and department are carried as evidence.
- No raw store or no market profile promotes nothing (never everything as a fallback); the source never reads the `technologies` table and never claims absence — an unobserved tool is simply not emitted.
- Registered in `src/sources/__init__.py`, enabled in `config/sources.yaml` (24h cadence) and documented in README/ENRICHMENT.

### Careers-page discovery is sitemap-first
- `AtsDiscovery` now reads robots.txt `Sitemap:` directives and walks the sitemap (or sitemap index), scoring every URL for careers-index shape, before falling back to a homepage link hop and then the hardcoded path guesses. New pure module: `src/identity/sitemap_careers.py`.
- The discovered careers URL is persisted even when no ATS is detected, so `ats_careers_page` scrapes the real index instead of a guessed path.
- New CLI command: `signals find-careers` (exported as `python -m src.cli find-careers`), which reports the URL and the rung that found it: robots_sitemap, root_sitemap, homepage_link or candidate.
- Sitemap-index children are ranked career-ish first, then generic page sitemaps (WordPress/Yoast `page-sitemap.xml`), then everything else, with taxonomy sitemaps last; gzipped children are skipped.

### Verified (offline lane)
- Full offline suite measured 2026-09-14: **1,962 tests collected — 1,954 passed, 8 deselected** with `-m "not antibot_live and not allow_network and not live_fetch"`. The live-marked tests (`antibot_live`, `allow_network`, `live_fetch`) are deselected in that lane.

## v0.2.0 — 2026-09-02 (P3 Polish delivery)

P3 roadmap complete: 16 polish items shipped after the P2 platform delivery
(28 tasks, Aug 31). Full suite: 1,287 tests green. CI green on
ubuntu-latest + windows-latest.

### Batch 1 — Foundations
- Injectable orchestrator clock (`set_today`), hardcoded `_today` removed (`8834511`)
- `sources.yaml` config lint in `doctor` — warns on keys no adapter consumes (`0c4ac05`)
- Run-scoped rotating log files matching run IDs (`f1fc52c`)

### Batch 2 — Sources & pipeline
- WARN notices: 4 new state parsers (NJ XLSX archive, FL reactwarn table, OH listing-only); MI deferred (JS-rendered) (`6e6f7e0`)
- Capterra consent-banner tolerance + review-id date-normalization parity (`cf476e3`, adoption fix `84f4a31`)
- Raw-store disk-usage quota guard in doctor/status; crt.sh/wayback jitter + backoff coverage; wayback cadence 336h (`1524e1a`)

### Batch 3+4 — Marketplace, lifecycle, delivery
- `MarketplaceReview` normalizing base + deep-reviews extreme-rating bounding (`fc405d3`, contract fix `8404701`)
- Doctor config/engine/browser validation (`e37f9f9`)
- Superseded-signal expiry — per-type `supersede_days`, soft-filter at scoring (`09efe64`)
- Per-tier alert routing + export destination plugins (`4ab9211`)
- Persona-variant briefs + tier-4 nurture digests (`3c5c170`)
- Review-fix wave: tier-4 flag wiring, supersede-aware digests, destinations wiring, deep-reviews null contract, defensive config parsing (`f1f9175`, `8404701`)

### Post-P2 feature (pre-P3)
- News relevance reranking — optional `ms-marco-MiniLM-L-6-v2` cross-encoder via `signals[rerank]` (`ae38bd2`…`29ecfcd`)

## v0.1.0 — 2026-08-31 (P1 + P2 delivery)

- P1: TrustRadius adapter, tech-stack change detection, review-velocity trends, Capterra slug discovery, G2 empty-since bookkeeping, confidence calibration, cross-source dedupe, GitHub Actions CI, Turnstile injection, DB migration system, durable scheduling, alert hardening, retention enforcement (13 commits `6e70203`…`f09f66e`)
- P2: 28 tasks in 7 batches — appstore_reviews + bbb_profile live, 4 ATS vendors + careers fallback, hiring-velocity trends, entity resolution, play backtesting, pricing-change detection, leadership NLP, health gate, signed webhooks, digests, email delivery, generic selfchecks, packaging consolidation (commits `e45e586`…`661cf4a`)

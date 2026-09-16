# TODO — Project Roadmap

Last reviewed: 2026-09-14. Baseline: **1,962 tests collected, offline lane:
1,954 passed, 8 deselected** (live-marked tests excluded). P1 fully delivered
(2026-08-31); **P2 fully delivered (2026-09-01)**; **P3 fully delivered
(2026-09-02)** — see below. Keyless Clay/Exa clones roadmap delivered
(2026-09-05). Legacy Cloudflare-bypass checklist archived at the
bottom.

Note from the P1 review sweep (P3 candidates): `flap_guard` persistence,
`effective_cadence` dead code, `_DELIVERED` eviction, raw-vs-blended confidence
audit trail, concurrent-watch JSON write locking.

Priority key: **P1** = high value / already-half-built · **P2** = solid value, more work · **P3** = polish.

---

## P1 — DELIVERED (2026-08-31)

All 13 P1 items shipped across 13 commits (`6e70203`…`f09f66e`), two-stage
reviewed (spec compliance + code quality per task), review-blocking fixes
applied before merge.

### Sources
- [x] **TrustRadius adapter** — full build: parser (real Slack fixture, 0-10→0-5 ratings), collector (`marketplace_trustradius`, `trrev:` keys, `source='trustradius'`), selfcheck branch, CF-bypass membership, config (disabled by default).
- [x] **Tech-stack change detection** — pure `diff_technologies()`; runner diffs prior cycle's technologies and persists `tech_install_new` (0.7) / `tech_churn` (0.6) candidates; flap-guard mechanism in place (dormant until prior-churn persistence lands — see P3).
- [x] **Review-velocity trend (`marketplace_review_trend`)** — count/rating deltas vs prior cycle from `data/marketplace/stats.json`; thresholds configurable (`review_trend:` in marketplace.yaml).
- [x] **Capterra slug discovery** — `resolve --capterra` via the server-rendered search endpoint; exact→normalized→substring ladder, ambiguous lists candidates and exits non-zero (spike `6e70203`).
- [x] **G2 "empty since" bookkeeping** — `EmptyLog` records consecutive empty cycles; cadence skips slugs in backoff (3+ empties); per-source keys (g2/capterra/trustradius).
- [x] **Confidence calibration** — `calibration` table (migration v2), `blend_confidence()` (no-op below 30 samples), wired into score via `calibration_stats` param; outcomes feed arrives with P2 backtesting.
- [x] **Cross-source event dedupe** — event-level correlation: marketplace natural keys + trend keys carry per-source/per-day identity; see also the confidence work.

### Pipeline / ops
- [x] **GitHub Actions CI** — `.github/workflows/ci.yml`: offline lane (windows+ubuntu matrix) on push/PR + nightly live-parity lane; no engine build in CI (curl_cffi fallback covers it).
- [x] **CF Turnstile token→cookie injection** — `inject_turnstile_token()` (navigate → add_cookies → reload → challenge-cleared poll); tier 3 end-to-end, tier-4 fall-through preserved. Mock-tested; live validation manual (needs 2Captcha key).
- [x] **Real DB migration system** — ordered `MIGRATIONS` with `PRAGMA user_version`, transactional apply, startup integrity check, corrupt-DB clear error.
- [x] **Durable scheduling** — `Scheduler` + `SingleFlight` (atomic O_EXCL lockfile, stale-break, own-pid release), jittered cadence, failure backoff (capped 8x), missed-run catchup; watch loop collects only due sources. OS cron still recommended for production.
- [x] **Alert delivery hardening** — URL validation, 3-attempt backoff+jitter (injectable sleep), delivery logging, 24h natural-key dedupe, configurable timeout.
- [x] **Data retention enforcement** — `prune` CLI (`--keep-days`, `--vacuum`): fetch_log/documents/runs rows + raw store files + WAL checkpoint + ANALYZE.

---

## P2 — DELIVERED (2026-08-31 → 2026-09-01)

All 28 P2 tasks shipped in 7 batches (37 commits `e45e586`…`ff81420`), source
spike first, two-stage reviewed per batch (spec + code quality), every review
major fixed and re-tested before the next batch. Excluded per product decision:
conference speaker/sponsor lists, CRM write-back.

### New sources (zero-cost ethos)
- [x] **Source-availability spike** (`4cd580d`) — paced live probes of 5 candidate hosts; verdicts re-scoped the batch (App Store + BBB GO; Reddit/YC/PH blocked → synthetic-fixture stubs with documented reasons).
- [x] **App-store reviews** — `appstore_reviews` live via iTunes RSS (`9ba85d9` wiring `34b33c1`); Play Store NO-GO (404+JS).
- [x] **BBB profile scraping** — `bbb_profile` live via server-rendered profiles (`d7788b9`); cross-account alt-name resolution emits `intent_3rd_topic`.
- [x] **Reddit / YC / Product Hunt** — parsers + collectors built against synthetic fixtures (`a2add32`, `66daf6c`, `6446c5c`); disabled by default with spike-referenced reasons (blocked / browser-tier / CF).
- [x] **Hiring-velocity trend signals** (`758a4f0`) — `hiring_trend_signal` diffs per-domain open-role counts cycle-over-cycle; emits existing `hiring_surge`; min-delta + min-count floors; stats JSON atomic.
- [x] **Job-board coverage beyond the 7 ATS adapters** (`22c7090`, wiring `b7a2bb6`) — `ats_rippling`/`ats_jobvite`/`ats_breezy`/`ats_teamtailor` + `ats_careers_page` fallback (fires only with no ATS vendor/token); breezy parser verified against a live probe.
- [x] **Hiring-trend pagination integrity** (`410b2f2`) — full-cycle job accumulation (fixes total-minus-page-1 baseline) + end-of-cycle mark-closed with the complete key set.

### Scoring & quality
- [x] **Entity resolution hardening** (`c6d98d8`, migration v4) — `normalize_entity`, difflib fuzzy match (0.87), `entity_aliases` table (manual/config only, never auto-merge); collision-tie fall-through.
- [x] **Play hit-rate backtesting** (`d4cca31`, migration v5) — `play_outcomes` (local, no CRM), `plays --outcome`, `plays-report`, `plays-calibrate` feeding the P1 calibration table (≥30 samples); distinct-pair rate math.
- [x] **Date normalization hardening** (`09230c1`) — to_iso_date strictness; unparseable dates keep raw strings instead of silent decay-floor flips.
- [x] **ICP-fit pre-scoring at seed** (`24ca8f9`) — `config/icp.yaml` rules evaluated before the single upsert; re-seeds preserve computed fit.
- [x] **Pricing-page change detection** (`5326439`, `410b2f2`) — pure `diff_pricing` on wayback snapshots; `pricing_change` signal; runner injects the prior pricing snapshot; conservative no-prev → no signal.
- [x] **Leadership news NLP deepening** (`8c8eb65`) — role buckets (revenue/product/tech/exec) + funding `amount_usd`/`stage` in evidence_data, sourced from the attribution-stripped headline only.
- [x] **Combo coverage selfcheck** (`4d837a5`) — `doctor` warns on emittable combos without COMBO_PLAY entries.
- [x] **Account health score** (`34b9668`) — pure polarity aggregation (`config/health.yaml` weights), play gating below threshold (suppressed families default `growth_pitch`); combo plays gated too.

### Delivery & integration
- [x] **Outbound webhook delivery** (`ebb3052`) — `post_json_webhook` with the hardened skeleton, HMAC `X-Signature` signing (sign-what-you-send byte contract), `deliver_alerts` dispatcher, `ALERT_WEBHOOKS_JSON` config (secret_env names only).
- [x] **Alert digests** (`db9e93e`, windowing `ee88519`) — daily/weekly per-account markdown with why-now lines from evidence_data; `digest --period` CLI to `data/digests/`.
- [x] **Email delivery channel** (`ff81420`) — stdlib-SMTP `send_email` (TLS default, env-var-name credentials resolved at send time, failures logged not raised); `digest --email` / `brief --email`.
- [x] **Selector-drift selfchecks for all scrapable sources** (`b44c221`) — generic five-state runner in `src/core/selfcheck.py`; `selfcheck --source techstack|news_rss|ats_greenhouse`; g2/capterra/trustradius wrappers byte-identical.

### Infra
- [x] **Packaging consolidation** (`efe194d`) — single dependency truth in pyproject (`pip install -e .`), `signals` console script, requirements.txt as thin export, CI updated.
- [x] **Split live-network tests from the unit suite** (`e45e586`) — conftest guard + `live_fetch`/`allow_network`/`antibot_live` markers + CI lanes.
- [x] **Watch-loop per-source isolation** (`181e15f`) — one adapter's exception no longer skips the rest.
- [x] **Observability: per-source health report** (`8f54279`) — `source_health()` aggregates fetch_log (fetched/cached/failed/error-class histogram/last success); surfaced in `status`.
- [x] **Formal error taxonomy & retry policy** (`88d6511`) — classified error_class on fetch_log (migration v3), per-class backoff multipliers, capped at 8× cadence; fanout failures stamp too.
- [x] **Cookie-jar rollout to all fetchers** (`05a7002`, persistence wiring `ee88519`) — core jar (per-scope files under `data/cookies/`) hooked into http/curl/browser fetchers behind `cookies.enabled` (default off); load-at-start/save-at-end.
- [x] **DB maintenance** (`f04d218`, migration v6) — hot-path indexes (fetch_log source+at), cookie-table expiry cleanup in `prune_all` (`cookies` counts key), redundant PK-covered index deliberately skipped.

### P2 review-fix ledger
`4b3c707` (guard regression), `c1feff7` (B1: tier window-neutrality + backoff cap + stub completeness), `410b2f2` (B3: hiring-trend full-page stats, ATS vendor gate, seed ICP preservation), `a7057db` (B4: pricing prev-snapshot injection, sign-what-you-send HMAC, hit-rate dedupe, health.yaml wiring, plays-calibrate), `ee88519` (B5: cookie-jar persistence, digest period windows), `3de6356` (v6 test reconciliation).

### Post-P2 addition: news relevance reranking (2026-09-01)

Inspired by donsetch's cross-encoder reranker (`ms-marco-MiniLM-L-6-v2` via
ONNX), adapted to Signals' single-SERP-query-per-account reality. Commits:
`ae38bd2` (signals[rerank] optional extra), `dd86f16` (rerank module —
NullScorer fallback, lazy OnnxScorer, pinned model revision), `22ddf82`
(rerank-aware candidate cap), `aa9a5b2` (news collector relevance floor +
evidence), `ad9573b`-series (review fixes: memoized scorer singleton, stable
sigmoid, loud degradation, rerank_live network-guard escape). Off by default;
enable via `signals[rerank]` extra + `rerank.enabled: true`.

---

## P3 — Polish

**Status: DELIVERED (2026-09-02)** — 16 items in 5 batches, two-stage reviewed
per batch, review majors fixed before the next batch. Commits `8834511`…`8404701`
(release: v0.2.0).

- [x] **Test date determinism** (`8834511`) — injectable orchestrator clock (`set_today`), hardcoded `_today` removed; autouse reset fixture (`fb549bf`).
- [x] **`sources.yaml` config lint** (`0c4ac05`) — `doctor` warns on keys no adapter consumes; shipped config lints clean.
- [x] **Logging file sink** (`f1fc52c`, `28221d8`) — run-scoped rotating files matching run IDs.
- [x] **WARN notices state coverage** (`6e6f7e0`, hardening `84f4a31`) — FL (reactwarn table, concatenated-cell split), NJ (XLSX archive), OH (listing-only, PDF details deferred); MI deferred (JS-rendered). 8 jurisdictions registered.
- [x] **crt.sh / wayback cadence tuning** (`1524e1a`) — jitter + backoff parity proven for both; wayback cadence 336h.
- [x] **`data/raw` disk-usage guard** (`1524e1a`) — `raw_quota_mb` (default off) surfaced in status + doctor.
- [x] **Capterra consent-gate + review-id stability** (`cf476e3`, `84f4a31`) — OneTrust tolerated; review-id hashes the normalized date with legacy-row adoption on scheme change.
- [x] **Marketplace schema unification + deep-reviews bounding** (`fc405d3`, `8404701`) — `MarketplaceReview` normalizing base; `deep_reviews_bound: extreme` default (null = legacy).
- [x] **Startup config validation** (`e37f9f9`) — doctor validates config keys, engine availability, browser install; never raises.
- [x] **`data/raw` disk-usage guard** — see above (`1524e1a`).
- [x] **Alert routing rules** (`4ab9211`) — per-tier routes (`ALERT_ROUTES_JSON`), legacy single-webhook unchanged.
- [x] **Brief personalization per persona** (`3c5c170`, persona regex fix `f1f9175`) — revenue/tech/exec framing from contact titles.
- [x] **Export destination plugins** (`4ab9211`, wiring `f1f9175`) — config-driven sinks reusing the signed delivery path.
- [x] **Superseded-signal lifecycle + tier-4 nurture digest** (`09efe64`, digest wiring `f1f9175`) — per-type `supersede_days` soft-expiry at scoring; `digest --tier-4` includes dormant accounts with no plays.
- [x] **Release/versioning process** (`f1f9175`…`8404701`) — CHANGELOG.md, v0.2.0 tag, CI install-smoke job.
- [x] **G2 NPS/helpful-votes markup discovery** — probe outcome (2026-09-02): no public selectors exist for the elv-* DOM (searched gists/actors/tutorials/design-system repo); re-probe requires live authenticated capture on a logged-in machine. Tracked below as a manual item.
- [x] **G2 deep-reviews strategy** — delivered as the extreme-rating bound above.

### Post-P2 feature (pre-P3): news relevance reranking
`ae38bd2`…`29ecfcd` — optional `ms-marco-MiniLM-L-6-v2` cross-encoder via `signals[rerank]`; off by default.

### P3 review-fix ledger
`fb549bf` (B1: adhoc funding clock bypass + autouse reset fixture), `84f4a31` (B2: review-id adoption, FL right-anchored split, quota in status, warn hardening), `f1f9175`/`8404701` (B3+4: tier-4 flag wiring, supersede-aware digests, destinations wiring, deep-reviews null contract, persona regex, defensive parsing).

---

## Enrichment upgrades — waterfall audit (2026-09-03)

Findings from auditing the shipped enrichment playbook (`ENRICHMENT.md`) for
functional gaps. Priority key as above. **Status: 8 of 9 delivered
(2026-09-03, two-stage reviewed, review blocker+majors fixed)** — commits
`fbc0b8d`…`5ad41c7`. WARN fanout wiring deliberately excluded by product
decision (Jason), stays open below.

- [x] **P1 — run the existing resolvers during sweep onboarding** (`fbc0b8d`) —
  new accounts get the CIK/ATS/feeds/ICP resolver pass automatically; skipped
  report recomputed post-resolve; `sweep --deep` extends it to existing
  accounts. Opt-in resolvers (appstore/bbb/linkedin) stay explicit flags.

- [ ] **P1 — wire the dormant WARN parsers into the fanout.** NJ/FL/OH/WA/TX/IL
  parsers exist and ship tested (`84f4a31`), but `warn_notices.plan()` fans out
  NY+CA only (`src/sources/warn/source.py:24`) — 7 jurisdictions of layoff
  signal are sitting dark. Fix: make the jurisdiction list configurable
  (`sources.yaml` → `warn.jurisdictions`, default the current two for
  conservative rollout), keep MI deferred (JS-rendered). **Excluded by product
  decision 2026-09-03 — revisit when more WARN coverage is wanted.**

- [x] **P1 — add `resolve --appstore` via the iTunes Search API** (`143f2e6`,
  wired `87bd6e6`) — candidates-only on ambiguity, never guesses.

- [x] **P2 — Form D stub accounts surfaced in digests** (`e828d3e` + digest
  wiring) — option (a): "New Form D issuers (unmatched)" section on non-domain
  digests, amount-sorted, capped 25. Option (b) champion-attach not attempted.

- [x] **P2 — prose-only combo recipes promoted to scored combos** (`63b7493`) —
  `turnaround_pitch` (intent_2nd_marketplace × exec_departure) and
  `relocation_window` (new_geo/department_expansion × layoff), with COMBO_PLAY
  mappings so doctor's coverage check stays green. Proxy types documented in
  the action text.

- [x] **P2 — `sweep --deep`** (`fbc0b8d`) — resolve → collect in one command.

- [x] **P3 — `resolve --linkedin` slug discovery** (`d3e3f86`) — one-row seed
  CSV through the companion scraper's `discover` subprocess (no login, no
  --headed, one subprocess per batch); slug read from the scraper DB.

- [x] **P3 — `resolve --bbb`** (`9150bef`) — BBB's JSON search API (the HTML
  page is a JS shell); candidates-only contract.

- [x] **P3 — company_feed URL hint from wayback** (`445bd73`) — feed discovery
  falls back to archived homepage snapshots (same-host feeds only) when the
  live site is a JS shell.

### Review-fix ledger (2026-09-03, `5ad41c7`)

BLOCKER: `marketplace_review_trend` was missing from `config/signals.yaml` —
the P1 review-trend feature's signals were silently discarded as
unknown_signal_type while stats_state advanced, burning every delta. Now a
real taxonomy type (neutral polarity, weight 18, marketplace_compare play)
with a persistence regression test. Majors: LinkedIn seed CSV deleted after
the subprocess (carried account names); sqlite busy-timeout 30s on the scraper
DB read. Minors: BBB reportUrl joined via urljoin (was string concat), `<em >`
markup tolerated in name matching, sweep resolver-pass scope comment,
feed-discovery budget named constant.

---

## Sources & waterfall-stability roadmap (2026-09-04 audit)

**Status: DELIVERED (2026-09-04)** — all 14 items in three subagent batches,
strict TDD per task, full offline suite green after each batch. Commits
`21489e6`…`0d9920a` plus the review-fix commit `33558bc`. The techstack DNS
probe question that opened the audit: **built and tested, not wired** —
`probe_dns()` / `dns_evidence_to_matches()` passed `tests/test_dns_probe.py`
with zero production call sites until this batch. Full plan:
`.zcode/plans/2026-09-04_160212-sources-waterfall-roadmap.md`.

### P1 — new functionality / stability
- [x] **Wire the techstack DNS probe into the collect pass** (`21489e6`) —
  `harvest_tech` merges MX/SPF/CNAME evidence via `merge_matches` after the
  `cloudflare_unsolved` reduction (DNS evidence is challenge-independent),
  gated on the html task, fail-open with a warning log. DNS-discovered vendors
  flow through `upsert_technologies` + the prior-cycle diff like HTML/HAR ones.
- [x] **GitHub token auth** (`fc07dac`) — `GITHUB_TOKEN` Bearer header attached
  in `plan()` via `FetchTask.headers` (same env var `config.py` consumes);
  unauthenticated behavior preserved when unset. The plan's "probe all org
  guesses" was deliberately **not** implemented at collect time: wrong-guess
  404s return `ok=False` and stamp backoff, so probing 2 wrong guesses per
  collect would trip the `fail_count >= 5` skip — multi-guess belongs in a
  future resolve-time resolver.
- [x] **Fanout-adapter isolation in `runner.run`** (`0098a85`) — `_run_fanout`
  wrapped in the per-account `_record_fail` pattern; one existing taxonomy test
  updated to the new no-raise contract. The review pass then removed the
  double stamp (`_run_fanout` stamped AND re-raised into the new catch —
  doubled `fail_count` and compounded backoff on every failed fetch).
- [x] **Failure backoff scales with real cadence** (`0edd8a8`) —
  `_record_fail(..., cadence_hours=)` fed from `adapter.cadence_hours` at all
  three call sites; the phantom `_default_cadence_hours` getattr deleted
  (wayback's 336h cadence now caps at 8×336h, 12h ATS sources no longer
  over-penalized).
- [x] **ATS detection/collector symmetry** (`e21481c`) — rippling/jobvite/breezy
  detection patterns added; bamboohr/jazzhr/personio detections now write
  `careers_url` only (leaving `ats_vendor`/`ats_token` unset keeps the
  `ats_careers_page` fallback eligible). Personio's XML board remains a free
  collector candidate.

### P2
- [x] **Per-source `rate_per_host` wired** (`c71cfea`) — the orchestrator feeds
  sources.yaml rates into the limiter as per-source claims; HttpFetcher passes
  `task.source`. The review pass re-keyed buckets to the HOST with min-claim
  semantics: private `source@host` buckets would have let sec_edgar 8.0 +
  sec_formd 8.0 sum to 16 req/s against data.sec.gov's documented 10 ceiling.
- [x] **G2 slug resolver candidates-only** (`82f5759`) — the first-result
  fallback removed; exact/unique-substring accepted, ambiguity reports
  candidates and persists nothing (the capterra contract).
- [x] **Resolver passes: per-account isolation + EDGAR index retry**
  (`7cf1eac`) — per-account try/except inside the edgar/appstore/bbb
  `resolve_all` loops (one bad row no longer zeroes a cohort pass);
  `refresh_index` retries once with injectable sleep, degrading to an empty
  index instead of raising.
- [x] **`renewal_window` wired** (`a060e20`) — emitted from the techstack diff
  pass using stored `first_seen_at`; per-vendor `contract_years` in
  fingerprints.yaml (workday: 3), default 1y; natural-key dedupe on re-runs.
  The review pass added a Feb-29 clamp (a Feb-29 `first_seen_at` previously
  raised ValueError every cycle for that domain, silently zeroing candidates).
- [x] **Alert dedupe: persist + evict** (`3627de1`) — atomic JSON store
  (`data/alerts_dedupe.json`), fail-open load at startup, >24h entries pruned
  on every write; delivery never breaks because persistence did.

### P3
- [x] **Concurrent JSON state locking** (`7a78e4a`) —
  `src/core/filelock.exclusive_lock` (SingleFlight-style O_EXCL, fail-open)
  wrapped around the three stats read-modify-write cycles; `EmptyLog.record_*`
  re-reads state inside the lock for a true RMW. The review pass made release
  ownership-checked (no unlinking a successor's stale-broken lock) and made a
  pid-write failure fail-open with cleanup.
- [x] **Marketplace empty-since for capterra/trustradius** (`9e4fe49`) —
  non-G2 marketplace adapters record empties/reviews per-source exactly like
  the G2 fragment path, so `_filter_backoff` is fully wired for all three.
- [x] **GitHub Feb-29 guard cleanup** (`68c8c73`) — the precedence-broken
  ternary replaced with a pure `_one_year_ago` helper (Feb 29 → Feb 28); the
  redundant inner ≥365-day re-check removed with no observable change on
  non-leap dates.
- [x] **New-source spike (probe-first)** (`0d9920a`) — verdicts from paced
  live probes (≤6 req/host, ≥5s apart; `scripts/source_spike_2026_09.py`,
  evidence in `data/probe/P3_SOURCE_SPIKE_2026_09.md`): **Trustpilot
  browser-tier** (AWS WAF JS challenge on both httpx and curl_cffi — skip
  unless reviews matter enough for a real-browser pass); **Gartner network
  GO via curl_cffi** (Software Advice `/<cat>/<slug>-profile/` and GetApp
  `/<cat>/a/<slug>/` are SSR with JSON-LD ratings — but low priority, the
  corpus overlaps Capterra); **usaspending.gov GO** (keyless JSON API:
  `POST /api/v2/recipient/` → hash, `GET /api/v2/recipient/<hash>/` profile,
  `POST /api/v2/search/spending_by_award/` with keywords+award_type_codes —
  a small collector is trivially scoped).

### Review-fix ledger (2026-09-04, `b9332ce` + `33558bc` + `adcc67f`)

BLOCKER (found by the README sweep, `adcc67f`): `tech_churn` was missing from
`config/signals.yaml` — `diff_technologies` had emitted it since the P1
change-detection feature, but `normalize_candidate` rejected every churn
candidate as unknown_signal_type, silently discarding the signal while the
technologies table still advanced. Registered (mirrors `tech_removed`,
displacement_pitch, weight 22) with a persistence regression test; taxonomy
count assertions updated to 42. Majors: fanout double-stamp (above); shared-host
rate min-claim (above).
Also: the rate-wiring commit's `source` kwarg broke the cookie-jar tests'
`FakeLimiter` stubs (18 failures) — stubs patched to tolerate extra kwargs
(`b9332ce`, house rule: stubs take `**kw`). Minors: renewal Feb-29 clamp,
filelock ownership + write-failure fail-open, silent DNS-merge/empty-since
excepts now log. Nit: spike script's `data/probe` mkdir moved into `main()`.
A standalone code-quality review pass over the combined diff confirmed the
rest clean (stub/real method parity, kwarg plumbing to real call sites,
natural-key dedupe, no silent-signal-loss paths).

---

## Core-source insights roadmap (2026-09-05)

**Status: DELIVERED (2026-09-05)** — all 16 tasks in five subagent waves plus
parent-side prep, strict TDD per task, full offline suite green after each
wave and at the final gate. Commits `5794876`…`49bc896` (24 task/fix commits).
Plan: `.zcode/plans/2026-09-04_204432-core-source-insights.md`.

### Prep — taxonomy
- [x] **9 new signal types registered parent-side** (`5794876`, `6e34a10`,
  `75ceaa3`) — positioning_change, bankruptcy_signal, contract_terminated,
  new_subdomain, github_momentum, federal_contract_award, reputation_drop,
  insider_trade (+ templates, counts 42→50, primary_types literal). Doing this
  up front kept every wave off signals.yaml and absorbed the count-assertion
  ripple in one place.

### Batch 1 — already-paid-for insights
- [x] **Wayback homepage positioning diff** (`ef6ff41`) — stored homepage
  snapshots now diff title/meta-description → `positioning_change` (strictly
  prior doc vs current, `poschg:{domain}:{today}`). Also fixed a latent crash:
  `follow_tasks` fed snapshot HTML to the CDX JSON parser, aborting wayback
  passes (pricing signals included).
- [x] **Appstore review-trend wiring fix** (`b4d3e9a`) — the only enabled
  review source's trend signal was dead (missing product_slug + the runner
  garbling `appstore_reviews` → `iews`); also captures app version, and the
  shared stats path now tolerates dict-shaped reviews.
- [x] **8-K items 1.03 + 1.02** (`23f2f2d`) — bankruptcy (chapter captured)
  → `bankruptcy_signal`; material-agreement termination →
  `contract_terminated`.
- [x] **WATCH_FORMS cleanup** (`48b93c9`) — 10-Q and SC 14D9 no longer
  fetched-then-dropped.
- [x] **usaspending.gov collector** (`97aaf69`) — new enabled `federal_contracts`
  source: per-account POST award search (trailing 12 months), never-guess
  recipient matching (ambiguous → nothing), `federal_contract_award` with
  humanized amounts.

### Batch 2 — dead types + new cheap sources
- [x] **Dead types wired** (`b1212ed`, `97c9acd`, `af0a379`) —
  `competitor_detected` from a fingerprints `competitors:` list (ships empty);
  `tech_removed` from the runner's captured `upsert_technologies` gone list
  (two missing runs); `backfill_open` from re-opened job titles (120d window).
- [x] **Status-page polling** (`d0980d3` + review fix `49bc896`) — when the
  persisted DNS evidence shows `status.<domain>` → `*.statuspage.io`, the
  techstack pass polls `index.json` and emits `competitor_outage` per
  unresolved incident. Gate reads the snapshot (plan stays pure); starts one
  cycle after DNS evidence lands.
- [x] **crt.sh subdomain delta** (`145260b`) — cycle-over-cycle new-subdomain
  detection (`new_subdomain`, permanent keys, capped 50/cycle).
- [x] **GitHub momentum** (`91cd810`) — new-repo / star-surge / archived
  deltas from the already-fetched repo JSON (`github_momentum`, stats JSON
  under filelock, monthly keys).
- [x] **ATS job-text deepening** (`12ee5b8` + review fix) — case-insensitive
  vendor allowlist, required-stack phrasing, all matches per job; restricted
  to REAL migration pairs (a bare stack mention no longer renders "migrating
  from  to X").
- [x] **BBB rating delta** (`1715b35`) — grade downgrade / accreditation loss
  → `reputation_drop` (first observation = baseline; sparse extra_data merge).

### Batch 3 — SEC depth + marketplace family
- [x] **SEC Form 4 + SC 13D/G** (`ed0a337`, `77e1891`) — Form 4 XML fanout
  (10 docs/cycle cap, namespace-agnostic parser, net bought/sold) →
  `insider_trade`; SC 13D/G stakes → `ma_target` (amendments unwatched).
- [x] **funding_drought** (`6b6a1a1`) — 18-24 months silent after a
  funding_form_d → runway-pressure signal (once per account; reports under
  sec_formd).
- [x] **Reviewer-ICP join** (`9c168f1`) — icp.yaml `reviewer_titles` matched
  against persisted g2_reviews reviewer fields → `intent_2nd_marketplace`
  candidates (dark-but-wired; marketplace adapters ship disabled). SA month-
  precision dates fall back to today instead of being silently dropped.
- [x] **Header evidence + DNS raw delta** (`d9c6f77`, `500fb3e`) —
  FetchResult.headers → `meta["response_headers"]` → `header:` fingerprint
  match type; DNS evidence snapshotted into extra_data, unclaimed spf_include
  removals emit `tech_churn` (mail-vendor switch).
- [x] **Software Advice / GetApp adapters** (`e76a9ae`) — fixture-built from
  the live P3 spike (JSON-LD SSR), disabled by default, fall through the
  existing marketplace review dispatch.

### Review-fix ledger (2026-09-05, `f4514fd` + `49bc896`)
BLOCKER-class: `_prev_homepage_html` picked an arbitrary same-batch snapshot
as "previous" on the first wayback cycle — near-universal garbage
`positioning_change` signals with possibly inverted old/new, locked in by the
daily key. Fixed: the prior doc must be strictly older than the current
fetch. Majors/minors: drought block once-per-account + sec_formd stats
attribution; ICP month-precision observed_at validated (silent normalize
drop); statuspage gate moved to the persisted snapshot (plan purity — the
contract violation and nondeterministic dry-runs); harvest_tech statuspage
guard; migration-type truthfulness; dns_evidence upsert churn; SA employer
org out of reviewer_title. Also: runner-driven tests used local
`date.today()` against the runner's UTC clock — latent TZ flakes that fire
after ~18:00 local; all new test files now assert on the UTC date
(`f4514fd`). A standalone quality reviewer verified the rest clean
(FetchResult.headers constructor safety, both meta-merge sites, all ten
natural-key namespaces, purity across new parse paths, extra_data merge
coexistence).

---

## Keyless Clay/Exa clones roadmap (2026-09-05)

**Status: DELIVERED (2026-09-05)** — 13 tasks across 4 waves, one subagent per
task (sequential), two-stage review per wave (spec + quality) with review fixes
applied before the next wave. Full offline suite green after every task and at
the final gate (1,835 passing; `doctor --no-network` clean). Commits
`9c44be0`…`8d03138` (18 task/fix/probe commits). Plan:
`.zcode/plans/2026-09-05_205645-keyless-clay-exa-clones.md`. Product decision:
clone Clay/Exa functionality natively — **no Exa/Clay APIs** (README:9 stance
kept; free keyed accelerators GKG/GITHUB_TOKEN named as the exception class).

### Wave 1 — identity discovery (Clay "find the company" clone)
- [x] **T1 keyless identity probe spike** (`9c44be0`) — paced 6-rung probe:
  Wikidata `wbsearchentities`+`wbgetclaims` P856 GO (stripe.com; 4/5 top-5
  collisions force description-keyed picks), Wikipedia `prop=extlinks` GO,
  EKG 401 CREDENTIALS_MISSING (schema probe PENDING-CREDENTIALS), legacy
  kgsearch 403 gate confirmed, DDG ladder (plain 202-challenge dead; curl_cffi
  chrome tier body-validated GO, stripe.com #1), robots status (Wikimedia
  `/w/` disallows `/w/api.php` → scoped overrides). Verdict:
  `data/probe/KEYLESS_IDENTITY_2026_09.md`.
- [x] **T2 identity_candidates review queue** (`5a15aee`) — migration v7
  `identity_candidates` (name, kind, candidates_json, chosen_domain,
  pending|accepted|rejected, source), `IdentityCandidateStore`
  (accepted-requires-domain validation), doctor `identity_candidates_pending`.
- [x] **T3a Wikidata + Wikipedia resolvers** (`0216c75`) — house-style
  (pure parse/pick, `_Task`, never-guess: exactly-one unambiguous survivor
  auto-accepts, else ranked display-only candidates); business-term filter;
  `robots_allow` scoped entries for both `/w/api.php` hosts.
- [x] **T3b GKG stage** (`04d1753`) — thin `KnowledgeGraphClient`: EKG default
  backend (`publicKnowledgeGraphEntities:Search`, service-account token,
  deferred `google-auth` import, `[gkg]` optional extra) + legacy kgsearch
  fallback (config-selected, off by default); derived-fields-only persistence
  (Google ToS); `gkg_backend` config knob; `gkg_ids` rate 0.5.
- [x] **T3c DDG stage** (`3916c00`) — `DdgSerpResolver` on the chrome-TLS
  curl tier (body-validated — challenge pages ship with 200/202 alike),
  strict #1+name-match auto-accept, process-wide `DDG_PACE_S` pacing,
  resolve-time only, off by default.
- [x] **T4 waterfall wiring** (`42dd6f8`) — `discover_waterfall`
  (wikidata→wikipedia→gkg→ddg, first-hit early exit, 2-source apex agreement
  promotion, per-stage error isolation) in `src/identity/discover.py`;
  `Orchestrator.discover` persists ranked candidates (normalize_entity keys);
  CLI `sweep --discover NAME [--ddg]` — never creates accounts; the binding
  bare-name refusal stays.

### Wave 2 — competitor intelligence (Clay find-similar clone)
- [x] **T5 G2 competitors probe** (`d23bede` + rerun `505c0e5`) — URL shape
  `/products/<slug>/competitors/alternatives`; plain curl tier 403 DataDome;
  browser tier challenge-blocked on stale session cookies after the
  chromium-1161 env fix; probing stopped at 3 fetches per the escalation
  discipline.
- [x] **T7 competitor news mining** (`867f292`) — Bing News RSS
  (`"X" vs` / `"X" alternative to`) + HN Algolia co-mention extraction →
  `CompetitorNewsPass` (3 GETs/name, per-source error isolation, score 1 =
  lowest tier, human gate mandatory) → `identity_candidates` kind=competitor;
  CLI `sweep --competitors NAME` (carries the deferred T6 mechanism).
- [x] **T8 list activation scaffolding** (`a0e8d4e`) — `config/lists/competitors.txt`
  (+ customers/dnc stubs, force-added) and the doctor `lists` check upgraded
  to WARN per missing icp.yaml-referenced file (resolution byte-identical to
  `load_domain_list`). Population is a human data step after approving queued
  candidates; `competitor_detected` + ICP disqualifier then fire unchanged.
- [x] **T6 (G2 page pass) DEFERRED** — needs a real DataDome capture (fresh
  browser cookies or solver keys); parser must not be written against guessed
  selectors. Recorded in `data/probe/G2_COMPETITORS_2026_09.md`.

### Wave 3 — signal catalog (Clay signals clone)
- [x] **T9 security_breach** (`05b2186`) — taxonomy 50→51 (both count pins;
  catalyst secondary), new `trust_rebuild_pitch` play, NewsRule
  (breach/ransomware/cyberattack/hacked/data-leak patterns; negatives for
  contract/insurance senses; ordered before competitor_outage), evidence
  template, emits tuple, news README, "data breach" serp keyword, real-path
  persistence + idempotent re-upsert test.
- [x] **T10 relocation emitter** (`713906e`) — `parse_bbb_profile` now extracts
  address (JSON-LD PostalAddress primary, `bpr-overview-address` fallback;
  verified on the real Avalara spike capture — the brief's named capture was a
  404 page, honestly recorded); `_relocation_delta` mirrors `_rating_delta`
  (extra_data baseline via task_meta, first observation silent, missing
  address never compares/erases); key `reloc:{domain}:{stable_id(new)}`.

### Wave 4 — delivery deltas (Clay delivery clone)
- [x] **T11 slack destination alias** (`76c2eeee`) — `type: slack` in
  `load_destinations` pins `format: "slack"` through the shared webhook path;
  `WebhookDestination` gained a digest-string branch (the Protocol's other
  documented shape previously crashed) reusing alerts primitives.
- [x] **T12 webhook signature options** (`7730e1f`) — per-webhook
  `signature: legacy|hub|stripe` (default byte-identical; hub = exact-body
  HMAC under `X-Hub-Signature-256`; stripe = `Stripe-Signature: t=,v1=` over
  `"<t>.<body>"` with injectable `now=` clock; unknown values warn + fall
  back).
- [x] **T13 docs + doctor sweep** (`f8090b6`) — README: identity-discovery
  section (waterfall, queue semantics, GKG credential setup, `--competitors`
  promotion), ethos line names the free keyed accelerators, DDG robots
  exception owned in prose, slack/signature delivery docs, 51-type catalog;
  `.env.example` GKG vars; doctor `gkg_credentials` check (WARN-only,
  network-free). Honesty gate: sweep --help, env vars, config keys, 51 types,
  v7 migration all verified against code (also fixed a stale "migrations v6"
  claim).

### Review-fix ledger
- **Wave 1 review** (0 majors, 3 minors → `1811463`): DDG pacing was voided by
  per-name resolver construction → process-wide bookkeeping + reset seam;
  identity_candidates keys now normalize_entity form ("Acme Inc"/"acme" share
  a row); dead resolved-branch in `_run_discover` removed and `--ddg` without
  `--discover` is now a UsageError.
- **Wave 2 review** (0 majors, 3 minors → `32a5423`): fetched-ok-but-0-items
  sources now record `notes` (dry feed vs soft block triage); explicit
  `COMPETITOR_CAP` output bound; `MAX_FETCHES_PER_NAME` budget-drift guard
  (adding a 4th source fails loudly).
- **Final review** (0 majors, 1 fix `8d03138`): security_breach verb-form
  contract-dispute negatives ("breached its contract"). Ledger notes (no
  action, deliberate): news natural key = shared link-hash scheme (house
  convention; the plan's `breach:{domain}:{source_id}` sketch superseded);
  digest-string webhook posts are unsigned by design (slack path carries no
  secret — revisit if a digest-to-signed-webhook feature lands); relocation
  compares raw one-line address strings (mirrors `_rating_delta`; normalize
  if BBB re-render false positives appear).

### Environment notes
- patchright 1.51.3 pins chromium-1161 → `patchright install chromium` run
  (headless shell downloaded). google-auth deliberately NOT installed in the
  main venv (optional `[gkg]` extra; tests inject the token seam).

## Intel master intelligence flow + needs source (delivered)

**Status: DELIVERED (2026-09-14)** — 16 commits `ede8153`…`e6001a7`
(`git log --oneline 6b67e1b..HEAD`).

The `intel` master flow ships as one command over five ordered stages —
identity → collect → derive → score → package — coordinating the existing
capabilities behind a single account (`bdc057d`, `d6c76c5`). The package stage
writes a portable, evidence-linked dossier (`abffdbb`): `manifest.json`,
`dossier.json`, `dossier.md`, `evidence.jsonl` and `prompt.md`, where every
claim cites an evidence record. A new local-tier `needs` source emits
`need_statement` (first-party `company_feed` operational statements, `ba9544d`,
`c57711f`) and `required_stack_demand` (open postings requiring a served
vendor, `b55dd76`), promoting only observations that match the selected
seller-market profile (`ede8153`, `config/markets.yaml` + `src/intel/market.py`;
profile-matched signal types declared in `bef1b24`). The flow reports one
terminal outcome per source (`443c703`, `src/intel/coverage.py`) and consumes
the authoritative scoring snapshot exposed by `score(return_snapshots=True)`
(`617baed`) rather than re-deriving from the mutable tables. Verified end to end
offline (`e6001a7`).

### Run it offline

```bash
signals intel <domain> --name "<Company Name>"
```

`--dry-run` requires an account that already exists and only plans collection
(no collect/derive/score/package). Live-marked tests are deselected in the
offline lane (`-m "not antibot_live and not allow_network and not live_fetch"`).

### Deferred

- [ ] **Fill in `config/markets.yaml`** — the shipped default profile is
  **empty on purpose**, so nothing is promoted until the seller's offerings,
  buyer departments, served-problem phrases and served vendors are written in.
  This is the user's product decision, not missing code.
- [ ] **Embeddings / neural search over the stored evidence** — now that need
  text is persisted this becomes worth doing; not attempted here.
- [ ] **Generalise need extraction beyond first-party `company_feed`
  documents** — news bodies and careers-page HTML are not yet covered.
- [ ] **Automatic alias / name-variant discovery from EDGAR/news strings** —
  the highest wrong-company attribution risk; deliberately not attempted.
- [ ] **New scoring combos + re-pricing `need_statement` /
  `required_stack_demand`** with `plays_calibrate`, once real profile-matched
  examples have fired. **No new combos were added in this delivery.**
- [ ] **Stale historical `play_assignments` cleanup** — `score()` upserts new
  assignments but never deletes ones that ceased to apply, so a later consumer
  querying that table can still see obsolete rows. The dossier avoids this by
  consuming the snapshot instead, but the table itself is unclean.
- [ ] **Parent/child run-log linkage** — `intel` does not create a parent
  `runs` row; resolve/collect/score create their own rows.
- [ ] **Marketplace parity** — `marketplace_softwareadvice` and
  `marketplace_getapp` need seeded identifiers in `extra_data`, and
  `marketplace_g2`/`capterra`/`trustradius` are browser-tier, so they
  additionally require `browser.enabled` — an opt-in flag alone cannot enable
  them. LinkedIn stays human-triggered because of the companion account's
  restriction history.
- [ ] **Decide whether any co-occurrence deserves to become a visible persisted
  signal** rather than a combo bonus.
- [ ] **A runnable lint lane** — `ruff` is configured in `pyproject`
  (line-length 100) but is not installed in this venv, and CI's ruff step is
  warn-only and pip-installs it, so lint cannot be verified offline. This is
  **not** gate-enforced.
- [ ] **Coverage status vocabulary note** — `missing_requires` is run-scoped (a
  configured source did not run this pass because the account lacked a field it
  requires); it does **not** mean a tool or fact is absent. The end-to-end test
  tolerates exactly that key in `coverage.summary` and forbids any
  missing-style key in the analysis sections.

---

## Deferred — not scheduled

- **G2 competitor page pass (T6)** — blocked on a real DataDome capture: fresh
  browser cookies exported to `data/g2_cookies.json` or the solver keys
  (`DATADOME_SOLVER_API_KEY`); then rerun `scripts/probe_g2_competitors.py`
  (fresh 3-fetch budget), pin the parse surface, build the pass.
- **EKG first credentialed run** — enable the API + service account, capture
  the entity-level response schema (undocumented), pin the url path in
  `gkg_client.py`; the schema probe in `scripts/probe_wikidata_resolve.py`
  rung 3 is ready.
- **Multi-user server / web UI** — thin read-only API + dashboard over SQLite.
- **Google Trends interest signal** — category demand timing.
- **ASN IP→org enrichment** — hosting/CDN vendor resolution from IP ranges.
- **Bi-directional CRM sync** — read dispositions back to feed play-hit-rate backtesting.
- **Legal/regulatory exposure scoring** — account-level compliance-risk summaries from federal_register/WARN.
- **Reddit mirror fallbacks** — old.reddit .json mirrors if JSON endpoints block (blocked until `community_reddit` is enabled).
- **Headed-fallback unattended path** — DataDome tier 4 auto-retry with session capture.
- **ML-DSA sig-alg support** — track BoringSSL upstream; post-quantum sig-algs when supported.

## Intel live-smoke findings (2026-09-15)

Live `intel` runs against `glow.security` and `darktrace.com` found these issues; the
offline suite could not. Fix order agreed: 1 → 4 → 3 → 2 (plan: `.hermes/plans/`).

### 1. `needs` cannot see dedupe-relabelled documents — FIXED by 05fd5a0 (plus the Wave D follow/capture work)
`documents.source` records only the FIRST writer of a content hash
(`src/core/rawstore.py:49-64`); `feed_discovery` fetched Darktrace's RSS 12 s before
`company_feed`, so the row says `feed_discovery` and `iter_docs(source="company_feed")`
(`src/sources/needs/collector.py:109`) sees nothing. sha256(stored body) == doc_id, so the
body IS there; `SELECT count(*) FROM documents WHERE source='company_feed'` is 0.
Action: resolve first-party doc ids from `fetch_log` provenance, UNIONed with the
`company_feed`-labelled rows (prune deletes fetch_log rows past `--keep-days`).

### 2. `company_feed` text is teaser copy, not first-person prose — FIXED by ab31c28 + 50abdfd
Stored Darktrace RSS: 91,783 bytes / 91,670 chars / 100 items / 0 `need_statement` rows;
item summaries are ~200 chars each. Titles plus teaser copy, no "we are building" prose.
Action: follow a capped number of feed item links, store the article bodies, strip HTML
before extraction.

### 3. Workday job descriptions are never fetched — FIXED by 3875894 + c4c1496 + 1a38195 + 085ef27 + b0c4200
76/76 Darktrace jobs have `description IS NULL` and no job anywhere has a description
>200 chars. `ats_workday` stores only the list endpoint; `parse_workday`
(`src/sources/ats/workday.py:49-74`) reads title/externalPath/locationsText/postedOn.
Consequence: `required_stack_demand`, `job_department` and the pre-existing jobsignals
required-stack work are all inert — a latent gap this feature exposed, not a regression.
Action: fetch each posting's CXS detail URL (already stored as `jobs.url`) under a cap.

Status 2026-09-15 (delivered): the live C1 probe captured a real payload (`3875894`,
`tests/fixtures/ats/workday_job_detail.json`, 200 + `application/json`, 6,176 bytes) after
verifying no offline substitute existed (4 Workday docs, all list pages under `.../jobs`;
0 of 2087 stored raw bodies contained `jobPostingInfo`). `parse_workday_detail` renders
`jobPostingInfo.jobDescription` to 4,056 clean chars (`c4c1496`); the adapter follows up to
`detail_follow_max: 10` detail URLs per list page, with the loader in
`src/sources/ats/thresholds.py`, the config value and its lint allowlist (`1a38195`); the
detail branch carries `posted_at=None` so COALESCE keeps the list pass's date.

Two corrections the live payload forced, worth remembering: there is NO
`jobFamily`/`jobFamilyGroup`/`jobCategory` key, so `department` stays None — i.e.
`job_department` remains inert for Workday by DATA, not by bug; and `posted` is a BOOLEAN
(`true`), not a date — mapping it would have written `True` into `posted_at`.

C4's trend dedupe (`973c4d0`) stops a doubled count firing a false `hiring_surge`, but its
first cut keyed `cycle_jobs` too and lost the list pass's posted_at/location on first
insert; `085ef27` restored the merge (both passes' posts reach `upsert_jobs`) and `b0c4200`
proves the chain end to end.

### 4. `upsert_jobs` blocking detail fields — WITHDRAWN, not a defect
Investigated and disproved. `src/sources/ats/common.py:163-164` writes
`overwrite={"last_seen_at"}` and every other column falls through to `db.upsert`'s
`COALESCE(excluded.col, jobs.col)` (`src/core/db.py:591-594`), so a detail pass with a
non-empty description ALREADY lands on an existing row (probed directly). The remaining
edge is an EMPTY STRING (`COALESCE` treats `''` as present), handled by a non-empty-title
guard in the detail branch. Kept here so nobody re-investigates it.

### 5. Killed runs strand `running` rows — FIXED by 135333b + f14acab + f29977c
`RunContext.__exit__` (`src/core/runlog.py:74`) never runs when the process is killed.
`b676e311` (collect, 2026-08-24T15:34:13+00:00) is still `running` today; `5d838b5e`
(2026-09-15) was stranded by a tool timeout and finalized by hand during the smoke, and
`902c16ff` was seen running during the 2026-09-15 review.
Action: read-only detector in `health.py`, surfaced by `status`/`doctor`, repaired by
`signals prune` with a configurable age cutoff.

### 6. Global fanout sources are unbounded for a single-account run — FIXED by 5f602ed
As of 2026-09-15 the DB holds 329 accounts, 291 of them `cik%` stubs stamped
`seed_source='sec_formd'`. One `intel glow.security` pass (`sec_formd` is fanout: one
global plan, parsed per filer) appears to have created ~93 of them — that per-pass
attribution is UNVERIFIED after the fact (`sec_formd` now holds 1,136 documents, so the
pass cannot be isolated). `intel <domain>` is not domain-scoped for fanout sources
(`sec_formd`, `federal_register`, `warn_notices`).
**E2 measurement 2026-09-15 (one `intel darktrace.com --force` run):** accounts went
**329 -> 389 (+60)**, `sec_formd`-seeded **291 -> 356**, and the cycle made **189 live
`sec_formd` requests** -- the entire SEC filing universe -- for ONE company. The dossier's
coverage section does not mention any of it.
Options: (a) leave as designed; (b) exclude fanout sources from `intel` by default with
`--include-fanout`; (c) keep them but report the seeded count as a gap.
Recommendation: **(b)+(c)** -- `intel` is a single-account flow, so the three global sources
(`sec_formd`, `federal_register`, `warn_notices`) should be opt-in, with a gap line reporting the
skip and, when included, the accounts created.

### 8. Workday detail coverage is positional -- 10 of 83 per cycle -- FIXED by 0d0e7a1
E2 measured descriptions **0 -> 10**, exactly `detail_follow_max`, but the board holds 83
postings. Cause: details are emitted per page, yet `max_passes` defaults to 2, so only page 0's
detail tasks execute -- pages 1-3's details are queued in pass 1 and dropped when the loop ends.
Because the slice `postings[:cap]` is positional, the SAME leading postings win every cycle, so
the remaining ~73 roles may never receive a description.
Options: (i) accept 10/cycle; (ii) raise the ATS pass budget so every fetched page's details
execute (~30-40 GETs/cycle); (iii) rotate/select the detail slice across cycles (via the source
cursor, or by preferring postings that still lack a description) so all postings are covered over
time at no extra per-cycle cost. Recommendation: (iii).

### 9. The dossier does not say why `needs` promoted nothing -- FIXED by b9bae34 (premise CORRECTED)
**Correction (found by the implementer, verified by grepping the E2 dossier):** this finding as
originally written was WRONG. The E2 dossier already carried the gap
`no relevance vocabulary configured: market profile 'default' is empty; nothing can be promoted`
(present twice, in `dossier.json` and `dossier.md`). What was genuinely missing was the CONSEQUENCE
(`needs promoted 0 signals`), the honest count of first-party documents in scope, and the remedy
(`--market-profile <id>`), plus a branch that never invents a count when the DB handle is absent.
`b9bae34` adds exactly those -- reporting only; `needs`, the profile resolver and `_load_profile` are
unchanged, and a configured profile stays completely silent.

### 7. Raw `(today - observed).days` can be negative in five modules — FIXED by 78e339d
`src/signals/score.py:62` (decay), `tier.py:31` (buying window), `evidence.py:58`,
`combos.py:30`, `lifecycle.py:43` (the `>` guard is safe). Observed with `today` = local
2026-09-14 against `observed_at` = 2026-09-15 (UTC ahead). Only the artifact age was
**Decision (Jason): normalise the age reference to UTC once** — `78e339d`. One stdlib-only helper
(`src/core/timeutil.py::utc_today`) now supplies the age reference, and every producer routes through
it: `orchestrator._today()` keeps `_INJECTED_TODAY` authoritative and only its FALLBACK became UTC;
`orchestrator.py:397` (which called `date.today()` directly and so silently bypassed the injection
hook) now uses `_today()`; `evidence.py:81`'s local default became `utc_today()`. The five consumers'
arithmetic is deliberately UNTOUCHED — their guards already handle a genuinely future-dated
observation, so clamping in five places was unnecessary. Two tests that encoded the old contract had
to change: `test_date_determinism` asserted `_today() == date.today()` (the bug itself), and two
orchestrator/funding tests froze the `date` symbol that the injection hook now bypasses — both
verified against a pristine worktree as broken BY this change, not pre-existing.
Honest caveat recorded with the change: the five consumer assertions cannot fail pre-fix (the fix is
at the producer), so a behaviour-level probe carries the evidence — with the local clock pinned to
2026-09-14, `render_evidence` pre-fix rendered `Raised (on 2026-09-15)`; post-fix it renders
`Raised (today)`.

### Cross-cutting note
`documents.source` is a first-writer label over content-addressed storage; any consumer
filtering documents by `source` shares finding 1's blind spot. Grep for `iter_docs(` and
`FROM documents WHERE source` before adding another one.

### Delivery record (2026-09-15)

- **Delivered** — offline part of `.hermes/plans/2026-09-15_102154-smoke-findings-fixes.md` (Revision 2), 9 commits: `a967335` filed these findings (Wave 0); `05fd5a0` resolved first-party documents by fetch provenance (Wave A / finding 1); `135333b` + `f14acab` detect, report and surface stale `running` rows (B1/B2) and `f29977c` finalizes them during retention (B3 / finding 5); `dc5630e` added a pure `html_to_text` helper (C0); `ab31c28` follows capped first-party article links and `50abdfd` extracts from rendered text (D1/D2 / finding 2); `701bc56` proves an article body becomes a promoted need (D3).
- **Wave C delivered (finding 3)** — `3875894` captured the real CXS detail payload (live, one GET); `c4c1496` parses it; `1a38195` follows capped detail URLs (`detail_follow_max: 10` + `src/sources/ats/thresholds.py` + lint allowlist); `973c4d0` dedupes postings for the hiring-trend count; `085ef27` restored the list+detail merge that C4's first cut broke; `b0c4200` is the end-to-end proof that a description lands while `posted_at` survives.
- **New baseline** — `pytest --collect-only` → 2021 tests collected; `pytest tests/ -o addopts="" -q -m "not antibot_live and not allow_network and not live_fetch"` → 2012 passed, 8 deselected, 1 failed (~106 s) — the single failure is the known unmarked antibot flake below; GitHub CI on the final push (`b0c4200`) concluded SUCCESS, so it is local-only. Previous collected baseline: 1968.
- **Known flake (pre-existing)** — `tests/test_antibot_engine.py::test_probe_fingerprint_returns_endpoint_json` is an UNMARKED live TLS probe to `tls.peet.ws`: it passed inside the broad run and failed when run alone (`RuntimeError: tls handshake, os error 10060`). That file was last modified at `01e4bdd` and is untouched by these commits — a network flake, not a regression.
- **Blast radius** — 3 source files, 1 config value (`article_follow_max: 5`), 1 lint allowlist entry in `src/pipeline/health.py`, 1 orchestrator helper (`_company_feed_kind`), plus tests/fixtures. The new capped follow means a `company_feed` cycle can now fetch up to `article_follow_max` extra pages per account.
- **E2 live re-run 2026-09-15** (`intel darktrace.com --name "Darktrace" --force`, run `f8e81e8e`, 19:51->20:00 UTC, exit 0, dossier `data/dossiers/darktrace.com-20260915T200032474363Z-c5a49a76`): **finding 1** confirmed -- the provenance resolver returns 7 first-party docs, including one stored under `feed_discovery` that the old label filter could never see; **finding 2** confirmed -- `company_feed` documents 0 -> 6 (feed + exactly 5 articles = `article_follow_max`); **finding 3** confirmed -- descriptions 0 -> 10 (= `detail_follow_max`), `department` 0 as C1 predicted, `posted_at` present on 82/83 jobs (the C4 merge regression verified on live data); **finding 5** confirmed -- `stale_running: 1` correctly flags `b676e311` from 2026-08-24; the hiring trend recorded **76**, not 86/152, and emitted **no** `hiring_surge`. Two per-source failures (`crtsh` robots, `federal_contracts` HTTP 500) were logged and reported in the dossier as `failed: 2` -- correct, not a defect. See findings 6, 8 and 9 for what E2 exposed.
- **Open-item wave 2026-09-15** -- `5f602ed` makes the three global fanout sources opt-in for `intel` (new `--include-fanout`, `fanout = True` on `sec_formd`/`federal_register`/`warn_notices`, skip + account-count reported in the dossier gaps); `0d0e7a1` makes Workday detail coverage converge (the adapter skips postings the runner reports as already described, the runner injects that set and enforces `detail_follow_max` as a per-CYCLE per-account budget with dropped tasks logged, and `WorkdaySource.follow_passes = 3` gives the pages already being fetched a wave in which their details execute) -- measured 10 of 83 per cycle before, all 83 over ~9 cycles after, at 10 GETs a cycle; `5e85fe0` corrects the now-stale `health.py` comment; `b9bae34` reports the `needs`-promoted-nothing consequence, document count and remedy. A flaky time-of-day assertion in `tests/test_stale_runs.py` (fixed `NOW` vs the real clock -- it failed CI at 20:09 UTC) was fixed in `82daa9d`.
- **Baseline** -- `pytest --collect-only` -> 2057 collected; the offline lane passes with only the known unmarked antibot TLS probe flaking (2048-2049 passed per run).
- **Outstanding** -- NONE. All findings are closed: 1, 2, 3, 5, 6, 7, 8 and 9 fixed with tests; 4 withdrawn with its disproving probe. Local == origin/master, CI green.

## Manual checklists (human setup, not code)

- **2Captcha provider setup** — create account, key in `.env`, verify `TurnstileTaskProxyless` vs `AntiCloudflareTaskProxyless` against a real managed challenge, test headed fallback (`CLOUDFLARE_HEADED_FALLBACK=true`).
- **Cloudflare config checklist** — `browser.enabled`, `cloudflare.enabled`, `bypass_strategy: browser_first`, proxy consistency.
- **G2 NPS/helpful-votes markup capture** — live authenticated capture of a G2 reviews page to hunt per-card NPS/helpful-vote selectors (2026-09-02 research found no public selectors; elv-* classes are hashed per deploy).

## P3 remainder (needs engine or manual validation)

- **Happy Eyeballs live validation** — `eyeballs.rs` races v6/v4 but has no live dual-stack end-to-end test.
- **Capterra consent-gate EU verification** — OneTrust tolerance shipped (`cf476e3`); live EU-geo verification manual.

---

## Done (for reference — do not redo)

- G2 elv-\* parser rewrite + adapter format-detect + runner fragment path (headed + warm-up)
- Capterra scraper end-to-end (parser, adapter, runner, `source` column, `capterra-selfcheck`)
- Multi-slug accounts (G2 + Capterra)
- Selector-drift selfchecks: `g2-selfcheck`, `capterra-selfcheck`
- SignalsShadow (`src/antibot`): native BoringSSL engine, own HTTP/2 (Akamai byte-exact), temporal stealth (resumption/pooling/304), solve-and-bounce ghost, RouteState lifetime learning, Happy Eyeballs, persistent cookie jar, DataDome/CF waterfall integration
- Stealth-browser lifecycle fix (closed in runner finally), G2 warm-up gating, empty-vs-challenge classification
- 2Captcha DataDome solver tier (cookie-based; token injection stub remains P1 above)

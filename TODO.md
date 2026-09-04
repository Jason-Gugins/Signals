# TODO — Project Roadmap

Last reviewed: 2026-09-04. Baseline: **1,455 offline tests passing**. P1 fully delivered
(2026-08-31); **P2 fully delivered (2026-09-01)**; **P3 fully delivered
(2026-09-02)** — see below. Legacy Cloudflare-bypass checklist archived at the
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

### Review-fix ledger (2026-09-04, `b9332ce` + `33558bc`)

Majors: fanout double-stamp (above); shared-host rate min-claim (above).
Also: the rate-wiring commit's `source` kwarg broke the cookie-jar tests'
`FakeLimiter` stubs (18 failures) — stubs patched to tolerate extra kwargs
(`b9332ce`, house rule: stubs take `**kw`). Minors: renewal Feb-29 clamp,
filelock ownership + write-failure fail-open, silent DNS-merge/empty-since
excepts now log. Nit: spike script's `data/probe` mkdir moved into `main()`.
A standalone code-quality review pass over the combined diff confirmed the
rest clean (stub/real method parity, kwarg plumbing to real call sites,
natural-key dedupe, no silent-signal-loss paths).

---

## Deferred — not scheduled

- **Multi-user server / web UI** — thin read-only API + dashboard over SQLite.
- **Google Trends interest signal** — category demand timing.
- **ASN IP→org enrichment** — hosting/CDN vendor resolution from IP ranges.
- **Bi-directional CRM sync** — read dispositions back to feed play-hit-rate backtesting.
- **Legal/regulatory exposure scoring** — account-level compliance-risk summaries from federal_register/WARN.
- **Reddit mirror fallbacks** — old.reddit .json mirrors if JSON endpoints block (blocked until `community_reddit` is enabled).
- **Headed-fallback unattended path** — DataDome tier 4 auto-retry with session capture.
- **ML-DSA sig-alg support** — track BoringSSL upstream; post-quantum sig-algs when supported.

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

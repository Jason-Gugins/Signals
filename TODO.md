# TODO — Project Roadmap

Last reviewed: 2026-09-01. Baseline: **1,152 tests passing**. P1 fully delivered
(2026-08-31); **P2 fully delivered (2026-09-01)** — see below. Legacy
Cloudflare-bypass checklist archived at the bottom.

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

## Deferred — not scheduled

- **Multi-user server / web UI** — thin read-only API + dashboard over SQLite.
- **Google Trends interest signal** — category demand timing.
- **ASN IP→org enrichment** — hosting/CDN vendor resolution from IP ranges.
- **Bi-directional CRM sync** — read dispositions back to feed play-hit-rate backtesting.
- **Legal/regulatory exposure scoring** — account-level compliance-risk summaries from federal_register/WARN.
- **Reddit mirror fallbacks** — old.reddit .json mirrors if JSON endpoints block (blocked until `community_reddit` is enabled).
- **Per-source `resolve` commands** — `resolve --g2`/`--capterra` exist; others stay config-manual.
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

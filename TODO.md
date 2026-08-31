# TODO — Project Roadmap

Last reviewed: 2026-08-30 (3-reviewer sweep: sources, infra/ops, product).
Baseline: 705 tests passing. Legacy Cloudflare-bypass checklist archived below.

Priority key: **P1** = high value / already-half-built · **P2** = solid value, more work · **P3** = polish.

---

## P1 — Next up

### Sources
- [ ] **TrustRadius adapter** — `src/sources/marketplace/trustradius.py` is empty; build on the G2/Capterra pattern (parser + collector + selfcheck, `source='trustradius'` in `g2_reviews`). Third marketplace review source, architecture already proven twice.
- [ ] **Tech-stack change detection** — techstack fingerprints current stacks but nothing diffs against prior runs/wayback snapshots. Emit `tech_install_new` / `tech_churn` *change* signals; vendor removal = displacement-play ammo. Highest-value untapped signal — the data is already collected.
- [ ] **Review-velocity & sentiment trend (`marketplace_review_trend`)** — G2/Capterra capture reviews but not longitudinal metrics: count deltas, rating drops, complaint themes ("support" → support_pitch).
- [ ] **Capterra slug discovery (`resolve --capterra`)** — the numeric product id is not derivable from the slug; without a resolver every Capterra account is hand-seeded. Needs a search/listing lookup.
- [ ] **G2 "empty since" bookkeeping** — persist empty-slug checks so cadence backs off zero-review products (currently INFO-log only). Cheap; saves weekly browser-tier fetches.
- [ ] **Confidence calibration** — `confidence` is a free float set per adapter with no calibration; build per-source/per-signal-type calibration from measured hit-rates.
- [ ] **Cross-source event dedupe** — the same funding event landing via news + SEC + Form D counts multiple times against per-type caps; add event-level correlation before scoring.

### Pipeline / ops
- [ ] **GitHub Actions CI** — no `.github/` exists. Lint + full offline suite (`pytest -m "not antibot_live and not allow_network"`) on push/PR.
- [ ] **CF Turnstile token→cookie injection (finish solver tier 3)** — 2Captcha API call works; token→`cf_clearance` browser injection is a stub. Makes managed-challenge sites end-to-end.
- [ ] **Real DB migration system** — `_migrate()` is additive ALTER-only with no schema version or integrity check; adopt numbered migrations + `user_version`.
- [ ] **Durable scheduling** — `watch.py` is a naive in-process sleep loop; add OS-level scheduler docs or a durable scheduler (missed-run catchup, jitter, backoff) so cadence survives restarts.
- [ ] **Alert delivery hardening** — `alerts.py` is Slack-only; add URL validation, retry/backoff, delivery logging, dedupe/throttle.
- [ ] **Data retention enforcement** — `RawStore.prune(keep_days)` exists but nothing invokes it; add a `prune` CLI command + WAL checkpoint + `fetch_log`/`runs`/`documents` growth pruning.

---

## P2 — Then

### New sources (zero-cost ethos)
- [ ] **Reddit enablement** (`community_reddit`, currently disabled) — r/sales, r/SaaS, company subreddits via public JSON; own rate budget + drift check first.
- [ ] **App-store reviews (App Store / Google Play)** — public RSS/HTML feeds; mirrors marketplace architecture, no anti-bot. Product-led accounts.
- [ ] **Product Hunt launches / "X alternative" lists** — strong second-degree intent for the G2 ICP space.
- [ ] **YC company directory** — public batch lists give company→domain identity resolution + funded-this-batch timing; static HTML, trivially cheap.
- [ ] **Hiring-velocity trend signals** — jobsignals captures postings; compute per-account open-role deltas so "hiring up 40% in 30d" becomes a primary trigger.
- [ ] **Job-board coverage beyond the 7 ATS adapters** — Rippling/Jobvite/Breezy/Teamtailor; plus direct `/careers` page scraping fallback feeding existing `jobsignals` analysis.
- [ ] **BBB profile scraping** — complaints/accreditation changes as churn/competitor signals.
- [ ] **Conference speaker/sponsor lists** — "company attending/presenting" intent; same collector family as blogs/itunes.

### Scoring & quality
- [ ] **Entity resolution hardening** — same-company variants across sources; registry covers names but not fuzzy/alias resolution.
- [ ] **Play hit-rate backtesting** — log play assignments + outcomes; compute per-play conversion to tune weights (pairs with CRM write-back).
- [ ] **Date normalization hardening** — audit every parser's `to_iso_date` handling; unparseable dates silently hit the decay floor and can flip tiers.
- [ ] **ICP-fit pre-scoring at seed** — `icp_fit` defaults to 1.0 and multiplies everything; make seed-time ICP scoring real from `config/icp.yaml`.
- [ ] **Pricing-page change detection** — wayback diff of /pricing; price/plan changes map to ROI/premium plays.
- [ ] **Leadership news NLP deepening** — role-specific parsing (new CRO/CMO/CTO → persona plays) + funding stage/amount extraction into evidence_data.
- [ ] **Combo coverage selfcheck** — any combo id without a COMBO_PLAY entry silently yields no play; add a config validator.
- [ ] **Account health score** — aggregate positive/negative signal polarity; gate plays (don't pitch growth to a company with WARN + layoffs).

### Delivery & integration
- [ ] **CRM write-back** (Deferred list) — HubSpot/Salesforce: push score/tier/next-play as account properties + tasks for play openers.
- [ ] **Outbound webhook delivery** — generalize the Slack-only `ALERT_WEBHOOK_URL` to arbitrary JSON webhooks with signing + retry.
- [ ] **Alert digests** — daily/weekly per-account digests with "why now" summarization across signals.
- [ ] **Email delivery channel** — briefs/digests by SMTP/ESP, not just `data/briefs/` files.
- [ ] **Selector-drift selfchecks for all scrapable sources** — only g2/capterra have them; generic per-source selfcheck (ok/drift/empty/challenge/error) for techstack/news/ATS catches silent parser rot repo-wide.

### Infra
- [ ] **Packaging consolidation** — `requirements.txt` (pinned) and `pyproject.toml` (bare) drift; move deps to pyproject, add `[project.scripts]`, lockfile.
- [ ] **Split live-network tests from the unit suite** — markers exist but no CI lane separation; offline-fast vs nightly-live jobs + conftest guard against unmarked network tests. Stabilize `antibot_live` (retries, local fingerprint fallback, nightly-only).
- [ ] **Watch-loop per-source isolation** — one adapter exception can skip others in an iteration; wrap per-adapter collection.
- [ ] **Observability: per-source health report** — `runlog.py` + `fetch_log` capture runs but nothing aggregates success/error rates per source per cycle; surface in `status`.
- [ ] **Formal error taxonomy & retry policy** — http.py classifies 403s ad hoc; define an enum (challenge/timeout/dns/rate-limit/parse-drift) in `fetch_log` with per-class backoff.
- [ ] **Cookie-jar rollout to all fetchers** — `PersistentCookieJar` is only merged in `SignalsTransport`; wire into `core/http.py` replay, curl_fetcher, and browser contexts.
- [ ] **DB maintenance** — WAL checkpointing, `ANALYZE`, hot-query index review, cookie-table expiry cleanup.

---

## P3 — Polish

- [ ] **G2 NPS/helpful-votes markup discovery** — fields exist, return None (no per-card markup found Aug 2026); periodic re-probe to map selectors when G2 ships them.
- [ ] **G2 deep-reviews strategy** — `deep_reviews: true` is slow browser-tier; consider bounding (only ≥4/≤2-star reviews expanded).
- [ ] **"Empty since" persistence** — bookkeeping table (companion to the P1 cadence-backoff item) for multi-cycle empty products.
- [ ] **Capterra consent-gate handling** — OneTrust banner is EU-geo only today; tolerate `#onetrust-accept-btn-handler` + add a `consent` state to the selfcheck contract.
- [ ] **Capterra review-id stability** — ids hash (slug, reviewer, posted); pin date-normalization parity so re-renders don't churn natural keys into duplicate "new" rows.
- [ ] **TrustRadius/other-marketplace schema unification** — common `MarketplaceReview` base once TrustRadius lands.
- [ ] **WARN notices state coverage** — 5 state parsers (ca/il/ny/tx/wa); add fl, oh, mi, nj… (pure parsing work).
- [ ] **crt.sh / wayback cadence tuning** — longest cadences; add jitter + last-success backoff so slow public mirrors don't pin runs.
- [ ] **Reddit mirror fallbacks** — old.reddit .json mirrors if JSON endpoints block.
- [ ] **`sources.yaml` config lint** — validator warning on keys adapters don't consume (e.g. undocumented `serp_keywords`, `jobsignals` thresholds).
- [ ] **Per-source `resolve` commands** — `resolve --g2` exists; Capterra resolver is the P1 item; keep others config-manual.
- [ ] **ML-DSA sig-alg support** — track BoringSSL upstream; enable Chrome's post-quantum sig-algs when supported (non-PQ fallback currently in `tls.rs`).
- [ ] **Happy Eyeballs live validation** — `eyeballs.rs` races v6/v4 but has no live dual-stack end-to-end test.
- [ ] **Headed-fallback unattended path** — DataDome tier 4 needs manual intervention; bounded auto-retry with session capture so scheduled runs don't hang.
- [ ] **Startup config validation** — `doctor` validates env; extend to config keys, engine availability, browser install, DB integrity.
- [ ] **`data/raw` disk-usage guard** — size/quota alerting alongside keep-days pruning.
- [ ] **Release/versioning process** — changelog, tags, CI smoke of `pip install -e .` + `init`.
- [ ] **Test date determinism** — `orchestrator._today()` hardcodes `date(2026, 8, 16)`; inject clock.
- [ ] **Logging file sink** — rotating run-scoped log files matching `runlog.py` run IDs.
- [ ] **Alert routing rules** — per-cohort/per-tier routing (tier 1 → immediate, tier 3 → digest) instead of one global webhook.
- [ ] **Brief personalization per persona** — variant briefs (CRO vs CTO) from existing plays.yaml personas.
- [ ] **Export destination plugins** — destination interface (file, S3, Sheets, webhook) so new sinks are config-only.
- [ ] **Multi-user server / web UI** (Deferred) — thin read-only API + dashboard over SQLite.
- [ ] **Bi-directional CRM sync** — read dispositions back to feed play-hit-rate backtesting.
- [ ] **Superseded-signal lifecycle** — per-signal-type expiry so stale signals re-validate or drop.
- [ ] **Tier-4 nurture digest** — dormant-account cadence instead of one-off plays.
- [ ] **Google Trends interest signal** (Deferred) — category demand timing.
- [ ] **ASN IP→org enrichment** (Deferred) — hosting/CDN vendor resolution from IP ranges.
- [ ] **Legal/regulatory exposure scoring** — derive account-level compliance-risk summaries from federal_register/WARN.
- [ ] **2Captcha provider setup checklist** — create account, key in `.env`, verify `TurnstileTaskProxyless` vs `AntiCloudflareTaskProxyless` against a real managed challenge, test headed fallback (`CLOUDFLARE_HEADED_FALLBACK=true`).
- [ ] **Cloudflare config checklist** — `browser.enabled`, `cloudflare.enabled`, `bypass_strategy: browser_first`, proxy consistency; `rm data/state/session.json` no longer required (fixed: fresh browser contexts).

---

## Done (for reference — do not redo)

- G2 elv-\* parser rewrite + adapter format-detect + runner fragment path (headed + warm-up)
- Capterra scraper end-to-end (parser, adapter, runner, `source` column, `capterra-selfcheck`)
- Multi-slug accounts (G2 + Capterra)
- Selector-drift selfchecks: `g2-selfcheck`, `capterra-selfcheck`
- SignalsShadow (`src/antibot`): native BoringSSL engine, own HTTP/2 (Akamai byte-exact), temporal stealth (resumption/pooling/304), solve-and-bounce ghost, RouteState lifetime learning, Happy Eyeballs, persistent cookie jar, DataDome/CF waterfall integration
- Stealth-browser lifecycle fix (closed in runner finally), G2 warm-up gating, empty-vs-challenge classification
- 2Captcha DataDome solver tier (cookie-based; token injection stub remains P1 above)

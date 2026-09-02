# Changelog

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

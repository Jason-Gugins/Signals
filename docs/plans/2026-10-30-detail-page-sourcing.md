# Detail-Page Sourcing Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Execute the extraction-validation verdict — the corpus lacks need-bearing prose because the prose-bearing pages were never *fetched* — by sourcing real content (Greenhouse job descriptions, case-study/blog bodies) into the document store, bounded and cadence-gated, then re-harvesting to answer the question the whole arc has been building toward: **does need-bearing prose, once present, move G5's nouls?**

**Architecture:** SPIKE-FIRST (house rule for new sources). Task 1 is a parent-run, free HTTP probe with GO/NO-GO verdicts per arm that gate and re-scope the build tasks. Three arms, cheapest-first by expected yield: **(A) Greenhouse boards-api `content=true`** — the collector at `src/sources/ats/collector.py:44` already requests `content=true` and `parse_greenhouse` already strips job-content HTML (gates.py-verified shapes), so the arm may be nearly free if the live probe confirms the databricks board returns descriptions; **(B) blog bodies via the existing `company_feed` source** — one `blog_feed_url` account field (a `deepen`/seed edit, zero new code) if rillet publishes RSS; **(C) sitemap-driven site-content fetcher** — a new adapter for case-study/compare/solutions pages enumerated by the already-stored sitemap. All arms store into the existing raw store → `doccontent` seam → decide layer unchanged. Task 4 is the re-harvest that measures the buried question with real numbers.

**Tech Stack:** Existing fetcher/budget/cadence machinery only. No new dependencies. New source adapter follows "Add a source in 20 lines" (README): pure `parse_*`, `@register` SourceAdapter with plan+parse, `__init__` import, `config/sources.yaml` entry, fixture + tests.

---

## Verified context (2026-10-30 — trust these over re-derivation)

- **The extraction validation's verdict** (`data/probe/ACCEPTED_CLAIM_READ_20261013.md`, appended): exclusions fired live, envelope claims eliminated; the corpus holds almost no promotable prose because the sitemap *listed* the content pages without fetching them. **The promotion path requires detail-page fetching** — this plan is that fetch.
- **Greenhouse board API is already integrated**: `src/sources/ats/collector.py:44` fetches `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`; `parse_greenhouse` (greenhouse.py:12-23) already reads `j.get("content")` and strips HTML via `strip_html(unescape(...))`. The stored databricks envelope (source `ats_discovery`, doc `d8419437…`) has **no content fields** — so the discovery path fetched something else (the careers-page embedded JSON, 892 jobs with title/location/metadata only). The spike must establish: the board token, whether `content=true` returns real HTML descriptions for databricks' board, and why the corpus path diverges.
- **Per-job keys in the stored envelope** include `absolute_url: https://databricks.com/company/careers/open-positions/job?gh_jid=8805655002` — the gh_jid and the URL pattern are already in hand for a per-job fallback probe.
- **`company_feed` requires only `blog_feed_url`** on the account (`src/sources/news/collector.py:160-163`: `requires = ("blog_feed_url",)`, one FetchTask per account) — arm B is potentially a data edit, not code.
- **Sitemap in hand**: rillet's stored sitemap (doc `037a2f69…`) enumerates 376 `<loc>` URLs including `/case-studies/*`, `/blog/*`, `/compare/netsuite|quickbooks`, `/solutions/*` — the content-page URL list needs no discovery, only pattern filters.
- **doccontent seam (shipped, `37a5dcf`)**: `human_text(content_type, body, publisher_hint)` handles json_jobs/json_results/rss/sitemap/robots/html/text with per-item publisher matching; html→`html_to_text` is applied for RSS descriptions but **html/text passthrough leaves raw HTML untouched** — detail-page HTML bodies flow through the existing `html` kind (raw HTML reaches the gate today by design; html_to_text at the needs/retrieval layers handles rendering). If the spike finds raw-HTML descriptions degrading gate states, a `job_detail` extraction kind is the contingency (see Task 2's re-scope note).
- **Wrong-entity posture**: per-result/per-job publisher matching is live (Zibma precedent); new fetched docs inherit it automatically.
- **Budget/cadence machinery**: per-source cadence in `config/sources.yaml`, per-cycle detail budgets (the workday `detail_follow` precedent), `--skip collect` freezing corpora for harvests, `scripts/ab` + `src/lab` tooling with dual-hash joins (post-extraction packages join correctly).
- **Cost posture**: all fetches are HTTP-only (free); LLM spend occurs only in `--with-llm` harvests (~$0.25/domain at retrieval-arm rates).
- **Robots/ethics posture**: robots.txt honored by default; the spike includes a robots check per arm; public boards-api/case-study/blog pages are public business content — the README's legal paragraph applies unchanged.

## STOP gates

Task 1 produces per-arm verdicts. Any NO-GO arm is dropped or re-scoped **before** its build task dispatches (the source-spike-probe house rule: first-request-challenge ⇒ abort that arm; 200-but-contentless ⇒ NO-GO for that arm). If ALL arms NO-GO: the wave stops with the probe record — the sourcing conclusion becomes "the prose is not fetchable within posture," and the decide-lab inherits the finding.

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** `pytest --collect-only -q` (record; last known 2,849+); `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-30-detail-page-sourcing.md`; TODO Deferred entry ("Detail-page sourcing — fetch the need-bearing prose the extraction wave proved missing; spike-gated, plan: <path>").
**Commit:** `docs(plan): detail-page sourcing — spike-gated fetch of job descriptions + content pages`

### Task 1: SPIKE — probe all three arms (parent-run, free HTTP)

**Method (`tmp/sourcing_spike.py`, output `data/probe/SOURCING_SPIKE_20261030.md`):**

1. **Arm A — Greenhouse content:**
   - Discover the board token: probe `boards-api.greenhouse.io/v1/boards/databricks/jobs?content=false` (token candidates: `databricks`; fallback: parse from the careers page HTML the discovery flow already knows).
   - On token hit: `GET .../jobs?content=true` — record: HTTP status, body size, per-job `content` presence rate, one sample description (first 300 chars of stripped text), total stripped-prose bytes across jobs.
   - Per-job fallback: `GET .../jobs/{gh_jid}` for one jid from the stored envelope.
   - Robots: `databricks.com/robots.txt` vs the careers path + boards-api host.
2. **Arm B — blog RSS:**
   - Probe `rillet.com/blog/rss.xml`, `/feed`, `/rss`, `/blog/feed` (common paths) — a valid RSS with `<item>` bodies ⇒ GO via `company_feed` (`blog_feed_url` field), which fetches item content into documents with ZERO new code.
   - One case-study page GET (`rillet.com/case-studies/vsee-…`) — is the body server-rendered prose (GO for arm C) or a JS shell?
3. **Arm C — sitemap content pages:**
   - GET two `rillet.com/case-studies/*` and one `/compare/netsuite` page from the stored sitemap; assess prose presence (server-rendered vs shell), size, and robots.
   - Same for one `databricks.com/blog/*` URL (does databricks publish in a fetchable form?).
4. **Write the probe doc**: per-arm verdict (GO / NO-GO + reason), the numbers, and the **re-scope decision** for Tasks 2–4.

**Commit:** `docs(probe): sourcing spike — arm verdicts (A/B/C), re-scope decision`

### Task 2: Arm A build — Greenhouse content into the corpus (if GO)

**Likely shape (confirm against spike data):** the fix is in the **collection path that produced the contentless envelope** — either the ats_discovery/collector flow adopts `boards-api…content=true` for the board (the list call already carries it in collector.py — diagnose why the stored envelope lacks content: wrong URL, discovery-path divergence, or the board genuinely returns empty content), or a bounded per-job detail pass (workday `detail_follow` precedent) using the boards-api per-job endpoint. doccontent: extend `json_jobs` extraction so a job's `content` (HTML) contributes `html_to_text(content)` prose (capped, e.g. 2,000 chars/job, inside the existing newest-100 window) — the extractor already exists; this is an additive branch + tests.
**Files (expected):** `src/sources/ats/collector.py` or the discovery flow, `src/core/doccontent.py`, `tests/test_doccontent.py`, fixture updates. **Verify:** unit tests + the real-byte smoke (re-fetch is Task 4's harvest, not a test).
**Commit:** `feat(ats): greenhouse job content into the corpus (spike arm A)`

### Task 3: Arm B/C build — content pages into the corpus (if GO)

**Arm B (RSS exists):** NO new code — set `blog_feed_url` on the rillet account (seed CSV column or `deepen`), let `company_feed` fetch on cadence; verify documents land and doccontent/rss handles them (already shipped).
**Arm C (new adapter):** `src/sources/sitecontent/` — a SourceAdapter whose `plan()` reads the account's stored sitemap document, filters `<loc>` URLs by pattern list (config: `content_patterns: ["/case-studies/", "/blog/", "/compare/", "/solutions/"]`), caps per-cycle fetches (default 20, workday budget precedent), and `parse()` extracts title + `html_to_text(body)` prose into the text store. Register in `src/sources/__init__.py`, `config/sources.yaml` entry (cadence-gated, `enabled` per house posture), fixture + tests.
**Commit:** `feat(sources): site-content fetcher — sitemap-enumerated prose pages (spike arm C)` (and/or the arm B note inside Task 4's record if zero-code)

### Task 4: Re-harvest — the buried question answered (live, ~$0.50)

Growth pass first (`intel databricks.com --force`-style cycles / collect runs to pull the new content into the store), then `persist_claims: true` + `ab_ai_infra_gtm` re-add (temporary, restored in Task 5), both domains, enforce, `--skip collect` impossible here — collect IS the point; run full `--with-llm`. Measure and append to `data/probe/ACCEPTED_CLAIM_READ_20261013.md`:
- Documents gained (job-description docs, case-study docs), accepted-claim mix (prose vs metadata), **G5 noul distribution vs the 0.09–0.29 envelope baseline, promotions vs the standing zero**.
- The interpretation contract: promotions > 0 ⇒ the arc's end-state (honest gates + real material = promoted signals); nouls rise but < band ⇒ the decide-lab's band/criteria proposal with real evidence; nouls stay low on genuinely rich prose ⇒ claim-quality work (decide-lab Branch I extensions).
**Commit:** `docs(probe): sourcing validation — G5 nouls on need-bearing prose <pre> -> <post>, promotions <n>`

### Task 5: Cleanup + docs + TODO

Flags restored (`persist_claims: false`, profile removed); README sources section gains the new adapter if built; TODO delivery record (spike verdicts, what shipped, the G5 answer, spend).
**Commit:** `docs: detail-page sourcing — delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| No unbounded fetching | per-cycle caps + cadence (adapter config) — spike records sizes first |
| Robots honored | spike robots check per arm; adapter uses the standard fetcher tier (robots by default) |
| Exclusion guarantees survive | doccontent per-item publisher matching applies to new docs automatically (Zibma-class tests already pin it) |
| Catalog stays excluded | sitemap remains catalog; content pages are NEW docs, not sitemap re-interpretation |
| Honest bookkeeping | fetch counts in source stats; harvest diffs recorded in the probe doc |
| The buried question | Task 4's before/after G5 measurement on real prose |

## Risks

1. **Greenhouse content may be sparse for some boards** (boards choose what to expose) — the spike measures presence rate before any build; NO-GO ⇒ arm drops.
2. **Careers-page scraping posture**: the gh_jid URL path (databricks.com host) is a JS-heavy page — prefer the boards-api host (already integrated, robots-friendlier); the spike decides.
3. **New adapter scope creep** (arm C is the biggest build): the pattern-filter + cap design keeps it ~150 lines; house "20-line source" conventions apply.
4. **Harvest variance**: claim-mix shifts are directional; the deterministic proof is documents-gained counts + the staged-text content check (same discipline as the extraction validation).
5. **Cost**: Task 4 ≈ $0.50; fetches free; spike free.

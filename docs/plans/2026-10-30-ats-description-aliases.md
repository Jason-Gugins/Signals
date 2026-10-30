# ATS Description Aliases + HTML Branch Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Close the gap the two-layer ATS answer exposed — the parsers grab job descriptions but the claim pipeline only extracts Greenhouse's `content` key — by generalizing the `json_jobs` prose branch to the alias set (`descriptionPlain`, `descriptionHtml`, `description`), and adding the deliberate `html` extraction branch so stored HTML pages (sitecontent, company_feed, wayback-html, ats_discovery) reach the gates as prose instead of markup soup. Validated by a rillet-first harvest on its 610KB of in-store Ashby descriptions.

**Architecture:** Two small doccontent changes + one seam constant, then a harvest. Task 1 extends `human_text`'s json_jobs branch: per-job prose from the first non-empty alias key, uniformly treated as HTML (`html_to_text(unescape(...))[:2000]` — verified safe: `html_to_text` is idempotent on plain text and drops script/style). Task 2 adds the `html` branch at the same seam (kind=html → `html_to_text(body)`) — this fixes the sourcing wave's own product (sitecontent pages currently reach the gate as raw markup) and the ats_discovery careers-page doc. Task 3 validates on rillet (Ashby prose is the point). Task 4 records. **No decide-layer changes; per-item publisher matching and the newest-100 window are untouched.**

**Tech Stack:** stdlib only. Measured grounding below; no new dependencies.

---

## Verified context (measured 2026-10-30 — trust these over re-derivation)

- **The two-layer gap (this wave's reason):** ATS parsers read descriptions into `JobPost` (lever `descriptionPlain`/`description` at lever.py:39; workable `description` — its docstring records "live-verified 88/88 non-empty"; jobvite/breezy/rippling/teamtailor similarly), but the claim pipeline's `doccontent.human_text` json_jobs branch reads only `title/location/company_name` + Greenhouse's `content` key (added in commit `28bd817`). Measured consequence: rillet's `ats_ashby` envelope (doc `1ed1012f…`, content_type `application/json; charset=utf-8`, 610,727 bytes) holds **40 jobs, 40/40 with `descriptionPlain` (236,126 chars total)** plus `descriptionHtml` — and the current extraction yields **1,662 chars** of title lines.
- **The measured Ashby shapes:** job keys = `address, applyUrl, compensation, department, descriptionHtml, descriptionPlain, employmentType, id, isListed, isRemote, jobUrl, location, publishedAt, secondaryLocations, shouldDisplayCompensationOnJobPostings, team, title, workplaceType` — **no `company_name` key** (org is `None`; the extractor's line-builder must keep skipping absent parts, which it does). `descriptionPlain` head: `'WHAT WE DO\n\nRillet serves accounting and finance teams…'` — **clean plain text**. `descriptionHtml` head: `'<h1>What We Do</h1><p style=…>…'` — HTML needing unescape+strip. Publisher matching is unaffected: json_jobs matching keys on `company_name` (absent here ⇒ per-job matching can't judge ⇒ kept, current semantics).
- **The uniform-treatment proof:** `textutil.html_to_text` (textutil.py:238) drops script/style (`_DROP` regex), keeps prose, and is **idempotent on plain text** (docstring-verified + live: `'Real Prose\nActual content here.'` from a script/style-laden sample). So `html_to_text(unescape(alias))[:2000]` is one safe path for all four keys — Plain gets a harmless whitespace collapse, Html/escaped-HTML get properly stripped.
- **The html branch's measured blast radius** (sniffed over the live store): rillet **13 html docs** (11 sitecontent + 2 wayback), databricks **8** (3 wayback + 4 company_feed + 1 ats_discovery 861,886-byte careers page). These currently reach the gates as raw markup — including the sourcing wave's own sitecontent pages, whose 9,600-char gate heads are Webflow `<head>`/CSS soup (the spike measured: script residue survives naive stripping; `html_to_text` handles it, verified live above). **Why this branch belongs in this wave:** sitecontent docs reaching the gate as markup soup undermines the sourcing investment this wave's harvest depends on. Dual-hash joins (shipped, `c3abacb`) already handle the re-head drift for lab/ab tooling.
- **The seam:** unchanged from the extraction wave — `src/pipeline/intel.py:462-476` staging block; `human_text` is the single extraction point. The 120,000-char extracted-text cap stays (Ashby's projected ~80–90KB fits; greenhouse's 120KB already caps).
- **Expected harvest yield (projection, to be measured in Task 3):** Ashby alias branch ⇒ rillet's staged text goes ~1.6KB → ~80–90KB of real prose (40 job descriptions incl. "WHAT WE DO / Rillet serves accounting and finance teams…"); html branch ⇒ sitecontent/case-study prose replaces markup soup. Baseline to beat: the sourcing harvest's G5 nouls 0.08–0.13, 0 promotions, thin accepted pool.

## STOP gate

No STOP gate this wave — both changes are bounded extractor extensions with offline-verifiable contracts and a measured fallback (passthrough) already shipped. If Task 3's harvest shows Ashby prose failing to stage (extraction regression), that's a bug fix, not a scope stop.

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** `pytest --collect-only -q` (record; last known 2,878+); `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-30-ats-description-aliases.md`; TODO Deferred entry ("ATS description aliases + html branch — generalizes the json_jobs prose branch to the alias set; unlocks rillet's 610KB in-store Ashby descriptions; plan: <path>").
**Commit:** `docs(plan): ATS description aliases + html branch`

### Task 1: doccontent — alias-set prose branch

**Files:** Modify `src/core/doccontent.py`; test `tests/test_doccontent.py`.

**Spec:** in the json_jobs extraction, each job's prose source becomes the first non-empty key among `content`, `descriptionPlain`, `descriptionHtml`, `description` (in that order — greenhouse's `content` keeps priority), treated uniformly: `prose = html_to_text(unescape(prose_raw))[:2000]` (the 2,000-char per-job cap from `28bd817` stays; `html_to_text` idempotency makes Plain safe; unescape first because the measured Greenhouse form is double-escaped HTML). The job line becomes `"{title} — {location} — {company_name}\n{prose}"` exactly as the greenhouse branch already builds it (absent parts skipped — Ashby's absent `company_name` keeps lines clean). Everything else untouched: newest-100 window, per-job publisher matching, 120,000-char total cap, never-raise fallbacks.

**Tests (TDD — RED first):**
1. Ashby-shaped envelope (synthetic, measured keys incl. `organization: None`, `descriptionPlain` plain text, `descriptionHtml` HTML): staged text contains descriptionPlain prose; `descriptionHtml`'s tags/style/script do NOT survive; absent `company_name` produces no `None` in lines.
2. Priority pin: a job with BOTH `content` and `descriptionPlain` ⇒ `content` wins (greenhouse shape unchanged).
3. Idempotency pin: a `descriptionPlain` with no HTML markup ⇒ prose equals the plain text (whitespace-collapsed), no mangling.
4. Cap pin: a 5,000-char descriptionPlain ⇒ prose capped at 2,000.
5. Real-byte smoke (offline-safe, skip-if-absent): the stored rillet Ashby envelope extracts text containing "Rillet serves accounting and finance teams" (the measured descriptionPlain head) and text_len >> 1,662.
**Verify:** `pytest tests/test_doccontent.py -v`; then `pytest tests/test_lab_isolation.py tests/test_source_purity.py -q`.
**Commit:** `feat(core): json_jobs prose from the ATS description alias set`

### Task 2: doccontent — html branch at the seam

**Files:** Modify `src/core/doccontent.py` (`human_text`'s html kind); test `tests/test_doccontent.py`.

**Spec:** `kind == "html"` ⇒ `text = html_to_text(body)[:120000]` (script/style dropped — verified live; the existing per-kind cap applies), replacing raw-HTML passthrough. `kind == "text"` (and robots) stay byte-passthrough. Docstring: state WHY (sitecontent/company_feed/wayback-html pages reach the gate as prose, not markup soup; the ats_discovery careers page joins them) and the disclosed drift (measured: 13 html docs re-head on rillet, 8 on databricks; dual-hash join `c3abacb` handles tooling).

**Tests (TDD — RED first):**
1. A Webflow-shaped html body (head with `<style>`/`<script>`, nav boilerplate, real prose mid-page) ⇒ extracted text has prose, no script/style content, no tag fragments.
2. The ats_discovery-shaped doc (861KB careers page) ⇒ text_len bounded by the cap and prose-bearing (use a synthetic half-scale fixture).
3. Passthrough pins unchanged: text kind byte-identical; robots as-is.
4. Real-byte smoke: the stored databricks ats_discovery doc extracts prose (skip-if-absent).
**Verify:** `pytest tests/test_doccontent.py -v`; then `pytest tests/test_intel_llm_layer.py -q` (the seam consumes the new text transparently; existing fixtures are plain-text passthrough — green UNMODIFIED).
**Commit:** `feat(core): html docs extract to prose at the implement seam`

### Task 3: Validation harvest — rillet's Ashby prose meets the gates (live, ~$0.25)

`lab.persist_claims: true` + `ab_ai_infra_gtm` re-add (temporary, restored in Task 4); rillet.com **first** (Ashby prose is the point), databricks.com second; enforce, `--with-llm --skip collect`. Measure and append to `data/probe/ACCEPTED_CLAIM_READ_20261013.md`:
- **Deterministic:** the staged ashby doc's text contains descriptionPlain prose (join via the dual-hash helper); rillet's staged-text total jumps (~1.6KB → tens of KB).
- **Directional (variance-exposed — report, don't assert):** accepted-claim mix (do "WHAT WE DO" descriptions yield need-statement-class claims?), G5 noul distribution vs the 0.08–0.29 baseline, promotions vs the standing zero.
**Commit:** `docs(probe): alias-branch validation — rillet staged prose <pre> -> <post>, G5 nouls <pre> -> <post>`

### Task 4: Cleanup + docs + TODO

Flags restored (`persist_claims: false`, profile removed); TODO entry flipped to delivered with the numbers; README untouched (no user-facing config changed).
**Commit:** `docs: ATS description aliases + html branch — delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| All four description keys extract uniformly | priority + idempotency + cap tests; real-byte ashby smoke |
| Greenhouse shape unchanged | priority pin (content wins); existing greenhouse tests green UNMODIFIED |
| HTML docs reach gates as prose | Webflow-shaped + ats_discovery-shaped tests; script/style non-survival pinned |
| text/robots passthrough byte-identical | passthrough pins |
| No decide-layer changes | diff scope: doccontent + intel seam none beyond existing human_text call |
| Publisher matching untouched | matching tests green UNMODIFIED (alias branch doesn't touch matching) |
| The buried-question progresses | Task 3: rillet staged prose jump + directional G5 measurement |

## Risks

1. **descriptionHtml strip quality** (Webflow-style inline styles) — `html_to_text` verified to drop style/script and keep prose on the measured sample class; the tests pin it.
2. **Gate-head concentration** (40 jobs × 2,000-char prose ≈ 80KB vs the 9,600-char gate head — the gate sees the first ~4 jobs' prose): same disclosed behavior as greenhouse; G4/G5 are quote-anchored/full-text and unaffected. Newest-first ordering is the priority.
3. **HTML-branch re-head drift** (13 rillet + 8 databricks docs): dual-hash join handles lab/ab tooling; intel runs re-hash at the seam — no machinery needed.
4. **Ashby claims may still not promote** (the prose is employer-branding, not need statements) — the read's lesson applies: this wave buys the MATERIAL; the decide-lab judges whether it clears the band. The harvest reports honestly either way.

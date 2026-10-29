# Extraction-Before-Claims Implementation Plan

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Execute the accepted-claims read's fork decision — fix the *material*, not the gates: extract human content from machine envelopes before the decide layer sees them, exclude catalog formats and wrong-entity documents outright, and validate that the accepted-claim pool changes from envelope restatements to content prose.

**Architecture:** One new pure module, `src/core/doccontent.py` (format sniff + per-format extractors, no I/O), wired at the single seam where the implement stage decodes stored bodies (`intel.py::_implement_pass`'s `_decode_body` step). Envelope documents (ATS API JSON, app-store JSON, RSS) yield their human content as clean labeled text; catalog documents (sitemap XML) and wrong-entity documents (envelope publisher ≠ account) are excluded BEFORE the doc gate, with honest counts. Downstream (doc gate, G3, implementers, G4, G5) is untouched — it simply judges human prose instead of scaffolding. A side effect, disclosed: envelope docs' doc-gate state heads change (the old heads were scaffolding), so recorded state-hashes for those docs evolve — acceptable and disclosed, never a silent rewrite.

**Tech Stack:** stdlib only (json, re, xml.etree) — no new dependencies. Real envelope fixtures: the actual stored docs measured on 2026-10-29 (grounding below).

---

## Verified context (measured 2026-10-29 against the real stored envelopes; corrected per two-agent verification)

The accepted-claims read (`data/probe/ACCEPTED_CLAIM_READ_20261013.md`) found 0/79 accepted claims worth promotion; the provenance table traced ~80% of them to four machine formats. The real stored envelopes, measured:

1. **Greenhouse ATS JSON** (databricks, doc `d8419437…`): content_type `application/json`, **766,574 bytes, 892 jobs** (size corrected by review: 766,574, not 766,482). Per-job keys: `absolute_url, application_deadline, company_name, data_compliance, first_published, id, internal_job_id, language, location, metadata, requisition_id, title, updated_at` — **no `description` field** (prose lives on per-job detail endpoints, not in this list envelope). The implementer received ~191k estimate-tokens of which the human content is 892 title/location/entity lines. Extractor: one labeled line per job — `"{title} — {location} — {company_name}"` — **most recent 100 jobs by `updated_at`** (measured: the newest-100 lines total **9,553 chars** — under the doc-gate 9,600 head today; the cap binds in future envelopes only, and only on the GATE state — G4/G5 are quote-anchored/full-text and unaffected). `company_name` is also the wrong-entity guard's publisher signal, matched **per job** (see the review finding: the measured databricks iTunes doc has 4 results with 4 DISTINCT artistNames including third-party "Databricks Quiz" apps — doc-level majority matching is undefined on ties and would pass per-item wrong entities; per-item matching drops those correctly).
2. **iTunes Search JSON** (rillet, doc `548916c4…`): content_type `text/javascript`, 7,617 bytes, `results[0]` carries **`artistName: "Zibma Infotech"`** and a **347-char `description`** — the source of the five wrong-entity claims about "Rillet Education". Extractor: `description` as the text; `artistName` as the per-result publisher signal. The guard: per-result publisher vs account name/aliases mismatch ⇒ that result's content dropped (Zibma Infotech is not Rillet). Results with NO artistName are kept but counted `unjudged` (can't judge ≠ wrong).
3. **Sitemap XML** (rillet, doc `037a2f69…`): content_type says `application/rss+xml` but the body sniffs as `<urlset>` (376 `<loc>` URLs) — **detection must sniff the body, not the header**. A URL list has no claim-worthy content: catalog ⇒ excluded from the implementer entirely (the read's ~38 URL-slug claims die here).
4. **robots.txt** (rillet, doc `631332a7…`): 1,222 bytes of policy text. Extractor: pass through as-is (small, honest) — or flag `policy`; pass-through is fine.
5. **HTML/text** (the general case): **pass through unchanged — NO html_to_text at this seam** (review correction: routing markup-bearing html docs through html_to_text would change staged text for every html doc, beyond the wave's envelope scope; today's pipeline already feeds raw HTML to the gate, and html stays that way). Only sniffer-detected envelope formats get extraction.

**Where the seam is (corrected per review):** `src/pipeline/intel.py::_implement_pass` decodes stored bodies at **lines 462-476** (`_decode_body` call at :472 — the ONLY call site; the plan's earlier "~826-845" anchor pointed at the event/amounts code). Replace the decode there. `needs` and every other consumer keep their own text handling — and the design review verified `needs` is provably unpolluted (its `_first_party_doc_ids` only sees `company_feed`-provenance docs; the envelope docs are `ats_discovery`/`appstore`-sourced; measured in the DB). Consequence, disclosed at full size: extraction re-heads **every sniffer-classified doc** — measured 43/75 of rillet's and 163+/226 of databricks' stored docs (RSS is the dominant class), not just the four study envelopes — so previously recorded state-hashes for those docs will not match post-fix states (tooling compatibility handled in Task 2b).

**What stays out of scope:** G5 band (stays 0.30 — the read's verdict), per-job detail-page fetching (the real job-description prose is a source-level follow-up — this wave answers how much was buried), entity-alignment integration, any decide-layer change.

---

# Tasks

### Task 0: Baseline + tracked plan + TODO

**Steps:** `pytest --collect-only -q` (record); `git rev-parse --short HEAD`; copy this plan to `docs/plans/2026-10-29-extraction-before-claims.md`; TODO Deferred entry ("Extraction before claims — envelope-aware human-content extraction + wrong-entity/catalog exclusion; plan: <path>"), cross-referencing the customer-proof entry's prerequisite (extraction ships first).
**Commit:** `docs(plan): extraction before claims — envelope-aware human content + wrong-entity/catalog exclusion`

### Task 1: `src/core/doccontent.py` — sniff + extractors

**Files:** Create `src/core/doccontent.py`; test `tests/test_doccontent.py`.

**Spec:**
```python
def sniff(content_type: str, body: str) -> str:
    """kind ∈ {"json_jobs", "json_results", "rss", "sitemap", "robots",
    "html", "text"} — BODY-sniff first (the sitemap that ships as
    rss+xml content type is the lesson), content_type as tiebreaker."""

def human_text(content_type: str, body: str, *, publisher_hint: str | None) -> dict:
    """{kind, text, publisher_items, excluded, reason}
    - json_jobs  (body parses to {"jobs": [{title, location?, company_name?,
                   updated_at?, first_published?}, …]}): one line per job
                   "{title} — {location} — {company_name}", most recent 100
                   by updated_at (else first_published, else file order);
                   PER-JOB publisher matching vs publisher_hint (account
                   name + domain aliases): jobs whose company_name fails
                   the match are dropped and counted (wrong_entity_jobs).
    - json_results ({"results": [{artistName/description/trackName…}]):
                   PER-RESULT publisher matching on artistName — results
                   whose artistName fails the match are dropped and counted
                   (the Zibma case); results with a MISSING artistName are
                   KEPT and counted unjudged (can't judge ≠ wrong);
                   text = surviving descriptions (labeled by trackName).
    - rss:       item titles + descriptions, with item DESCRIPTION HTML
                   stripped to anchor text via html_to_text (measured:
                   google_news item descriptions are anchor soup around
                   news.google.com redirect URLs — verbatim extraction
                   would keep the URL-scaffolding class this wave kills).
                   Zero items parsed ⇒ fall back to text passthrough
                   (Atom/odd feeds: never silently empty).
    - sitemap:   {"kind": "sitemap", "text": "", "excluded": True,
                  "reason": "catalog_url_list"} — nothing to mine.
    - robots:    text as-is (small policy file).
    - html/text: body UNCHANGED (no html_to_text at this seam — review
                  correction: routing markup docs through html_to_text
                  would alter staged text beyond the envelope scope).
    Malformed JSON that sniffs json ⇒ fall through to text passthrough
    (never raise). All functions pure: body in, result out. Cap any
    extracted text at 120,000 chars (bounded implementer input)."""
```
Detection details (from the measured envelopes): JSON bodies → `json.loads`; then `jobs` key ⇒ json_jobs; `results` key with `artistName` ⇒ json_results. XML bodies: `<urlset` ⇒ sitemap; `<rss` or `<feed` ⇒ rss (parse `<item><title>/<description>`; zero items ⇒ text passthrough). `User-agent:` + `Disallow` ⇒ robots. `<html`/`<!DOCTYPE` ⇒ html. Default text.

**Publisher matching rule** (documented in the docstring): casefold both sides; match if either string contains the other as a substring, or any domain alias (from `registry.entity_aliases_for(domain)` — the resolve-time loader at src/pipeline/orchestrator.py:175-204, REUSED not reinvented) matches either side. The account name comes from the DB (`accounts.name` — verified: "Databricks", "Rillet").

**Fixtures:** build synthetic envelopes matching the MEASURED field shapes exactly (the grounding above is the contract), plus a real-byte smoke: load the actual stored greenhouse doc if present (skip when absent, offline-safe).

**TDD:** tests first (RED), then implement. `pytest tests/test_doccontent.py -v`. Purity: functions take strings, return dicts — no I/O, no clock (consistent with the sources purity discipline even though src/core isn't scanned).

**Commit:** `feat(core): format-sniffing envelope extractors (doccontent)`

### Task 2: Wire the seam + wrong-entity/catalog exclusion in `_implement_pass`

**Files:** Modify `src/pipeline/intel.py` (`_implement_pass` decode block, ~lines 826-845 where `_decode_body` is used); tests in `tests/test_intel_llm_layer.py` (new section) + `tests/test_doccontent_integration.py` if cleaner.

**Spec:** the decode step becomes (SEAM CORRECTED per review: the staging block is intel.py:462-476, `_decode_body` call at :472 — the only call site):
```python
info = doccontent.human_text(
    str(getattr(doc, "content_type", "") or ""), text,
    publisher_hint=account_name,   # account name + domain aliases inside
)
if info["excluded"]:
    excluded_catalog += 1      # honest count, stage note
    continue                   # never staged, never gated, never spent on
wrong_entity_items = info.get("wrong_entity_dropped", 0)
if info.get("publisher") and not publisher_matches(info["publisher"], account_name, aliases) \
        and info["kind"] in ("json_results",):
    excluded_wrong_entity += 1 # doc-level exclusion for results envelopes
    continue
text = info["text"]
if wrong_entity_items:
    excluded_wrong_entity_items += wrong_entity_items  # per-item count, doc included
```
- `publisher_matches(publisher, account_name, aliases)`: casefold; match if either contains the other as a substring, or any domain alias (from `registry.entity_aliases_for(domain)` — the resolve-time loader at src/pipeline/orchestrator.py:175-204, REUSED not reinvented; account names verified in DB: "Databricks", "Rillet") matches either side. Zibma Infotech vs Rillet ⇒ no match ⇒ excluded. Per-ITEM matching handles the measured databricks iTunes doc (4 results, 4 distinct artistNames incl. third-party quiz apps — doc-level majority matching was undefined on ties and would pass wrong entities).
- Exclusions happen BEFORE staging, BEFORE the doc gate, BEFORE any Jev spend. Counts land in the stage record (`excluded_catalog`, `excluded_wrong_entity_docs`, `excluded_wrong_entity_items`) and the gap-note interaction is handled per review: when exclusion counters are nonzero, the "0 bodies loadable; stage no-op" gap line is suppressed or reworded (exclusions ≠ unloadable bodies — the review caught the misleading interaction at intel.py:477-483).
- State-hash drift, disclosed at full measured size: extraction re-heads EVERY sniffer-classified doc — 43/75 of rillet's and 163+/226 of databricks' stored docs (RSS is the dominant class) — so previously recorded state-hashes for those docs will not match post-fix states. Tooling compatibility is Task 2b.
- Nothing else in `_implement_pass` changes: the doc gate, G3, implementers, G4, G5 all receive the extracted `text` exactly as they received decoded text before.

**Task 2b: dual-hash join compatibility (the review's major finding).** `scripts/ab/report.py::staged()` and `src/lab/corpus.py::_DocIndex` reconstruct gate states by recomputing `state_hash` over the RAW stored body head — post-extraction runs hash over EXTRACTED text, so new packages' gate rows would silently fail to join (measured drift surface: 43/75 rillet docs, 163+/226 databricks docs; corpus rows would read `doc_joined: False`, backfill candidates would vanish, A/B staged counts would undercount). Fix: both recomputers index TWO hashes per doc — the raw-body head hash AND the `doccontent.human_text`-extracted head hash — and a gate row joins if it matches EITHER (pre-extraction runs join via raw; post-extraction via extracted). `src/lab/backfill.py` inherits the fix through corpus. Tests: a package with pre-extraction rows and one with post-extraction rows both join; the two-hash index is built offline-safe.

**Tests:** a world with the three real-format envelope docs (greenhouse json, itunes json with mismatched publisher, sitemap) + one html/text doc: staged text for the text doc unchanged (byte-identical — html/text pass through untouched); greenhouse doc's staged text is the clean title lines (metadata/gdpr/absolute_url absent); itunes doc: the Zibma result's description ABSENT from staged text, the "Databricks Inc." result's description present, `excluded_wrong_entity_items` counted (doc included — per-item matching); sitemap EXCLUDED with `excluded_catalog` counted; all-docs-excluded world ⇒ the misleading "0 bodies loadable" gap does NOT fire (reworded/suppressed per review). All existing intel tests green UNMODIFIED (their fixtures are content_type-less plain text — verified by review).

**Verify:** `pytest tests/test_intel_llm_layer.py tests/test_doccontent.py tests/test_intel_retrieval_staging.py -v`; then the wide lane `pytest tests/ -k "intel or decide or llm" -q`.

**Commit:** `feat(intel): extraction-before-claims — human content at the implement seam; catalog + wrong-entity exclusion`

### Task 2b: Dual-hash join compatibility (the review's major finding)

`scripts/ab/report.py::staged()` and `src/lab/corpus.py::_DocIndex` reconstruct gate states by recomputing `state_hash` over the RAW stored body head — post-extraction runs hash over EXTRACTED text, so new packages' gate rows would silently fail to join (measured drift surface: 43/75 rillet docs, 163+/226 databricks docs; corpus rows would read `doc_joined: False`, `src/lab/backfill.py` candidates would vanish, A/B staged counts would undercount — the plan's original "no tooling breakage" claim was wrong and is superseded).

**Files:** `src/lab/corpus.py`, `scripts/ab/report.py`, `tests/test_lab_corpus.py` (+ a small test in `tests/test_lab_runner.py` if the runner asserts joins).

**Spec:** both recomputers index TWO hashes per doc — the raw-body head hash (pre-extraction compatibility) AND `truncate(doccontent.human_text(content_type, body)["text"], 9600)`-hashed (post-extraction) — keyed per doc; a gate row joins when its `state_hash` matches EITHER. The extracted branch runs offline-safe (doccontent is pure; NullEmbedder-irrelevant). Pre-extraction packages join via raw exactly as before (no behavior change); post-extraction packages join via extracted.
**Tests:** a package with pre-extraction rows and one with post-extraction rows both join; a doc whose raw head ≠ extracted head joins via its correct branch; corpus rows carry `doc_joined: True` in both worlds.

**Commit:** `fix(lab+ab): dual-hash doc join — pre- and post-extraction packages both reconstruct`

### Task 3: Validation harvest (live, ~$0.50) — the buried-question answer

Temporarily re-add `ab_ai_infra_gtm` + `lab.persist_claims: true`; `SIGNALS_DECIDE_MODE=enforce`, rillet.com + databricks.com, `--with-llm --skip collect`. Then measure vs the accepted-claims read's baseline. **Assertions, ordered by determinism (per review):**
- **Deterministic (decision-grade):** stage record shows `excluded_catalog ≥ 1` (rillet) and `excluded_wrong_entity_items ≥ 1` (the Zibma results); no URL-list docs staged ⇒ slug claims structurally impossible; the Zibma description absent from staged text ⇒ "Rillet Education" claims structurally impossible.
- **Directional (LLM-variance-exposed — report, don't assert):** accepted-claim mix shifts toward content prose; G5 nouls + promotions re-measured (does human prose move G5's verdicts at all? — the buried question). A **near-empty rillet claim pool is a valid outcome** if the stage record shows the exclusions fired — that is the read's prediction ("wrong-entity claims are worse than no claims").
Append to `data/probe/ACCEPTED_CLAIM_READ_20261013.md` (the harvest validates THAT read's prediction). **Commit:** `docs(probe): extraction validation — exclusions fired, envelope claims <n> -> <n>, G5 nouls <pre> -> <post>`

### Task 4: Cleanup + docs + TODO

Flags restored (`persist_claims: false`, profile removed); TODO: fix-wave entry gains the extraction delivery + the customer-proof prerequisite status update; README one line if a user-facing behavior changed (envelope docs excluded from the implement stage — the honest gap line covers it).
**Commit:** `docs: extraction-before-claims — delivery record`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| Envelope scaffolding never reaches the implementer | Task 2 integration test: gdpr/metadata/absolute_url keys absent from staged text |
| Wrong-entity docs never reach G4 | itunes-mismatch exclusion test; validation harvest Zibma claims = 0 |
| Catalog formats never reach G4 | sitemap exclusion test; validation harvest URL-slug claims = 0 |
| HTML/text docs byte-identical behavior | passthrough tests; all existing intel tests green UNMODIFIED |
| Honest bookkeeping | exclusion counts in stage record + gap note; state-drift disclosed |
| Decide layer untouched | diff scope: intel.py decode block only; gates.py/jev.py diff empty |
| First content-prose claims | Task 3 harvest: accepted-claim mix shifts, with the buried-question answer |

## Risks (amended per review — several original risk claims were corrected)

1. **Thin greenhouse extraction** (title lines only — no descriptions in the list envelope): measured newest-100 lines = 9,553 chars. The claims get cleaner but not deeper. The real prose needs detail-page fetching — a source-level follow-up this wave explicitly stages. The Task 3 harvest quantifies what was buried.
2. **State-hash drift, full measured size**: extraction re-heads every sniffer-classified doc (43/75 rillet, 163+/226 databricks — RSS-dominated), NOT just the four study envelopes. Handled by Task 2b's dual-hash join.
3. **Publisher matching false positives/negatives**: per-item artistName matching is the design (doc-level majority was undefined on ties — the measured databricks iTunes doc has 4 distinct artistNames); missing artistName ⇒ kept + counted unjudged (conservative).
4. **100-job cap**: binds only the GATE state in future longer-title envelopes (measured: newest-100 = 9,553 chars, under the 9,600 head today); G4/G5 unaffected (quote-anchored excerpts + full extracted text to the implementer). Newest-first is correct priority.
5. **Sniff misclassification** (a JSON doc that isn't jobs/results falls to text passthrough) — safe direction: passthrough is today's behavior.
6. **Greenhouse size note**: stored envelope is 766,574 bytes (review corrected the plan's 766,482).

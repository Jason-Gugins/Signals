# Name-Resolution Test Expansion Plan — EDGAR accuracy + generic names

> **For the implementer:** Use the subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Close the name-accuracy blind spots the user called out — EDGAR filing name matching and generic/ambiguous company names — by pinning the never-guess contract with offline characterization tests, fixing the one latent wrong-company bug they expose (`_by_entity_alias` tie), and recording every behavior we deliberately do NOT change as a deferred decision.

**Architecture:** One offline test-expansion wave. Task 1 pins `match_cik`'s fuzzy arm (default 0.9) and its collision/None paths. Task 2 drives multi-filer generic-name collisions end-to-end (resolver wiring + Form D attach). Task 3 is the wave's only production change: a tie-guard in the entity-alias legacy scan, mirroring `_normalized_name`'s documented policy. Tasks 4–6 pin the known limitations (`name_matches_domain` prefix rule, waterfall generic-name behavior, the two normalization stacks' divergence). Task 7 records. **No thresholds change, no normalization unification, no alias auto-discovery — those are recorded decisions, not code.**

**Tech Stack:** stdlib + pytest only. Fully offline; zero live fetches, zero budget.

---

## Verified context (measured 2026-10-30 — trust these over re-derivation)

- **Two parallel normalization stacks.** `src/identity/names.py` (EDGAR/CIK matching; suffix set includes sa/srl/bv/ab/oy/plc/pty/lp/llp/pllc/ag/nv/spa/pte — names.py:10-37) vs `src/identity/resolve.py` (`normalize_entity`; only inc/llc/ltd/corp/co/gmbh/incorporated/limited/corporation/company — resolve.py:19-21). Same input, different keys depending on the code path.
- **`match_cik` three arms** (src/identity/edgar_ids.py:57-85): exact ticker casefold (multi-hit → None), exact normalized name (multi-hit → None), then fuzzy `name_similarity >= 0.9` where **a single surviving CIK auto-accepts**. The 0.9 bar is high: `{a,b,c,d}` vs `{a,b,c}` token sets score 0.75 Jaccard + 0.15 prefix bonus = 0.90 exactly.
- **`name_similarity`** (names.py:66-87): token-set Jaccard, +0.15 when one normalized name is a prefix of the other, +0.08 first-token tie, exact normalized = 1.0.
- **`name_matches_domain`** (names.py:101-119) is the shared "same org" scorer for ALL waterfall stages + DDG strict rule. Its prefix rule (117-118: `label.startswith(compact) or compact.startswith(label)`, only a len≥3 guard) makes **"Signal" match signalfire.com** and its first-token rule (115) makes **"Delta" match deltafaucet.com** — real false-positive vectors, deliberately left unchanged this wave.
- **Latent wrong-company bug (the one fix).** `AccountRegistry._by_entity_alias` legacy scan (src/identity/registry.py:223-227) returns the FIRST row whose alias normalizes equal — **no tie guard**, unlike `_normalized_name` (registry.py:146-166) which documents the policy: ties → None ("must NOT resolve to whichever row comes first"). Two config rows "Acme East"→a.com, "ACME EAST LLC"→b.com make `resolve(name="acme east")` nondeterministically pick a.com. Form D attach (src/pipeline/funding.py:457 via src/sources/sec/formd_identity.py:28) consumes this path — a funding signal can silently re-point.
- **Dead code note:** `fuzzy_match` (difflib 0.87, resolve.py:51) is imported in registry.py:11 but never called — production fuzzy is `name_similarity` at 0.86 (registry) / 0.9 (match_cik). The test_entity_resolution.py:61-79 boundary tests protect nothing real.
- **EDGAR fixtures:** tests/fixtures/sec/company_tickers.json (5 entries incl. `AMAZON COM INC`). Discover tests monkeypatch `discover._run_*` (tests/test_discover_waterfall.py:5-7) with one real-fetcher end-to-end at 457-484. Offline gate: `.venv/Scripts/python.exe -m pytest -m "not antibot_live and not allow_network and not live_fetch" -q` (conftest.py:53-81 blocks httpx/curl_cffi/urllib/socket).
- **Concurrent session active** (ATS alias plan landing on this tree — HEAD moved mid-research). Stage ONLY files this plan's task touched, by explicit path, and re-check `git status` before every commit. Never stage: BUILD_NOTES.md, config/*, docs/server-architecture-exploration.*.

## STOP gate

None — fully offline, no budget, no thresholds changed. Two downgrade rules: (a) if Task 3's tie-guard breaks any existing test beyond the new ones, stop Task 3, keep the RED test as `xfail` with the finding recorded; (b) if Task 5's Wikipedia disambiguation guard ripples beyond `wikipedia_ids` tests, downgrade to an xfail pin + deferred entry.

---

# Tasks

### Task 0: Baseline + TODO

**Steps:** record `pytest --collect-only -q` count (last known 2,878+), `git rev-parse --short HEAD`, `git status --short` snapshot (the not-ours list above). Add the TODO.md Deferred entry: "Name-resolution test expansion — EDGAR match_cik contract, multi-filer generic-name collisions, waterfall ambiguity pins; `_by_entity_alias` tie-guard fix; plan: docs/plans/2026-10-30-name-resolution-tests.md". Check `git diff TODO.md` shows only this entry before staging.
**Commit:** `docs(plan): name-resolution test expansion — EDGAR accuracy + generic names`

### Task 1: match_cik contract tests — fuzzy arm, collisions, degenerate inputs

**Files:** extend `tests/test_edgar_ids.py`.

**Tests (characterization — write to the current contract; every fuzzy-arm test first sanity-asserts the similarity band so the case is self-documenting):**
1. Single fuzzy survivor at the real default 0.9 attaches: account `"Acme Alpha Beta Gamma"` vs index `"ACME ALPHA BETA INC"` (tokens {a,b,c,d}/{a,b,c} → 0.75 + 0.15 prefix = 0.90) → returns that CIK. Sanity: `name_similarity(...) >= 0.9`.
2. Just-below miss: same index, account `"Acme Alpha Gamma"` (Jaccard 2/4 + 0.15 < 0.9) → None. Sanity: `name_similarity(...) < 0.9`.
3. Two fuzzy survivors ≥0.9, distinct CIKs → None (never-guess). Construct two index rows scoring ≥0.9 against one account (prefix bonus both ways); verify with the sanity asserts.
4. Generic short name misses: `"Acme"` vs `"ACME GLOBAL HOLDINGS INC"` → None (similarity ~0.4 — the conservative direction).
5. Ticker collision: two rows, same ticker, different CIKs, ticker arm → None.
6. `account` with `name=None, ticker=None` → None, no crash.
7. Punctuated EDGAR title `"Acme (Delaware)"` matches account `"Acme Delaware Inc"` (normalize strips parens) → exact arm.

**Verify:** `.venv/Scripts/python.exe -m pytest tests/test_edgar_ids.py -v` (existing 6 + new, all green).
**Commit:** `test(edgar): match_cik contract — fuzzy 0.9 arm, multi-hit None paths, degenerate inputs`

### Task 2: Multi-filer generic-name collision, end-to-end

**Files:** create `tests/fixtures/sec/company_tickers_collision.json` (~12 rows: Acme/Signals/Delta variants across distinct CIKs, same schema `{cik_str, ticker, title}`); create `tests/test_edgar_name_accuracy.py`.

**Tests:**
1. Through `match_cik` with the collision fixture: `"Acme"` → None; `"Signals Inc"` → None; `"Delta"` → None; `"Delta Air Lines Inc"` → its row (exact normalized).
2. Through `EdgarIdentityResolver.resolve_all` with a FakeFetcher serving the collision fixture (mirror tests/test_resolver_isolation.py's FakeFetcher pattern): ambiguous accounts get **no CIK written**, unique ones do; run stats report skips/fails honestly.
3. Form D attach safety: two existing accounts whose aliases normalize identically ("Acme East" / "ACME EAST LLC", different domains) + `attach_form_d_account(name="Acme East Co", ...)` → **stub account**, not a wrong attach (pins the `_normalized_name` tie → None fall-through under the Form D path, src/sources/sec/formd_identity.py:28).

**Verify:** `pytest tests/test_edgar_name_accuracy.py tests/test_formd_identity.py tests/test_resolver_isolation.py -v`.
**Commit:** `test(edgar): multi-filer generic-name collisions end-to-end + Form D attach pin`

### Task 3: FIX — `_by_entity_alias` legacy-scan tie-guard (TDD, the wave's one production change)

**Files:** modify `src/identity/registry.py:223-227`; extend `tests/test_entity_resolution.py`.

**Step 1 — RED:** two entity_aliases rows, same normalized alias, different domains ("Acme East"→alpha.com, "ACME EAST LLC"→beta.com) → `registry.resolve(name="acme east")` must return **None** (fall through), currently returns whichever row is first. Run: `pytest tests/test_entity_resolution.py -k tie -v` — expect FAIL.
**Step 2 — GREEN:** mirror `_normalized_name`: collect distinct domains from the legacy scan; `len==1 → self.get(...)`, else `None`. Keep the exact-key fast path (registry.py:218-222) unchanged.
**Step 3 — priority pins (characterization, must stay green):** exact-key hit beats legacy scan; name-alias beats entity-alias for the same normalized key; tie-guarded entity-alias falls through to `_normalized_name` then `_fuzzy_name`.
**Verify:** `pytest tests/test_entity_resolution.py tests/test_identity_candidates.py -v`, then the full offline gate.
**Commit:** `fix(identity): entity-alias legacy scan returns None on ambiguous tie`

### Task 4: name_matches_domain known-limitation pins

**Files:** extend `tests/test_names.py` with a `TestKnownLimitations` class; docstring cites this plan.

**Tests (pin current behavior — no code change):**
1. `"Signal"` vs `signalfire.com` → True (prefix rule; the documented false positive).
2. `"Delta"` vs `deltafaucet.com` → True (first-token rule).
3. Must-stay-True positives: `"Acme Corp"`/`acme.com`, `"Northwind Data"`/`northwind-data.com` (hyphen collapse), `"Signal.ai"`/`signal.ai`, 3-char brand `"Iox"`/`iox.com`.
4. Both-false negatives: `"Acme"` vs `widgetworks.com` → False.

**Bookkeeping:** TODO.md Deferred gains "name_matches_domain boundary policy (prefix/first-token false positives on generic brands) — product decision".
**Verify:** `pytest tests/test_names.py -v`. **Commit:** `test(identity): pin name_matches_domain prefix/first-token false-positive class`

### Task 5: Waterfall generic-name contract tests

**Files:** extend `tests/test_discover_waterfall.py`, `tests/test_wikidata_ids.py`, `tests/test_wikipedia_ids.py`, `tests/test_ddg_ids.py`.

**Tests:**
1. Realistic ambiguous generic: Wikidata stage returns Delta Air Lines + Delta Faucet (both business, both P856) → `ambiguous`, both ranked, nothing fabricated; `discover_waterfall` status `ambiguous`, no promotion.
2. Filter-does-real-work: "Acme" search returns one business-with-P856 + one color-band entity → `resolved` to the business (the `_is_business` filter earning its keep on generic names).
3. BUSINESS_TERMS substring false-survivor: entity described "…software for signal processing" with P856 survives as business → pin the risk.
4. Wikipedia disambiguation first hit: `hits[0]` title `"Delta (disambiguation)"` → must NOT resolve. **If RED:** minimal guard in `src/identity/wikipedia_ids.py` pick path (skip titles containing "(disambiguation)"); if it ripples, xfail-pin + record instead.
5. Apex-agreement tie: two domains, two stage-votes each → pin the tie-break (STAGE_PRIORITY, discover.py:143-151).
6. DDG: #1 result on a `_NON_OFFICIAL` root that still `name_matches_domain` → stays ambiguous (ddg_ids.py:227-257 strict rule).

**Verify:** `pytest tests/test_discover_waterfall.py tests/test_wikidata_ids.py tests/test_wikipedia_ids.py tests/test_ddg_ids.py -v`.
**Commit:** `test(identity): waterfall generic-name ambiguity contract — realistic payloads, disambiguation, apex tie`

### Task 6: Normalization-stack divergence pin

**Files:** create `tests/test_name_stacks_divergence.py`.

**Test:** one parametrized table over shared inputs ("Acme S.A.", "Acme SPA", "Acme B.V.", "Acme Pte", "Acme Co Ltd", "Acme Two", "Acme 2", "the Acme Corporation") asserting what `names.normalize_name` vs `resolve.normalize_entity` each return — computed at authoring time, pure characterization, passes by construction. Docstring: WHY the stacks exist separately and that unification is a recorded product decision.

**Verify:** `pytest tests/test_name_stacks_divergence.py -v`. **Commit:** `test(identity): pin the names.py / resolve.py normalization divergence`

### Task 7: Findings record + TODO + full gate

**Steps:** write `data/probe/NAME_RESOLUTION_TESTS_20261030.md`: gap→test map, the Task 3 RED→GREEN record, the deferred-decision list (boundary policy, stack unification, `parse_submissions_identity` unused hook — the natural feed for the still-deliberately-unattempted alias auto-discovery, TODO.md:662). Flip the TODO entry to delivered; add the new deferred entries. Run the full offline gate.
**Commit:** `docs(probe): name-resolution test expansion — findings + deferred decisions`

---

## Validation matrix

| Invariant | Proof |
|---|---|
| EDGAR never-guess under generic names | Task 1+2: collisions → None, misses conservative, stub-not-steal |
| Fuzzy-arm behavior is pinned, not assumed | Task 1 sanity asserts on `name_similarity` bands |
| No silent wrong-company alias path | Task 3 tie-guard RED→GREEN + full suite green |
| False-positive class is documented, not hidden | Task 4 known-limitations class + TODO entry |
| Waterfall ambiguity contract holds on realistic payloads | Task 5: Delta/Delta, disambiguation, apex tie |
| Stack divergence is visible, not latent | Task 6 table + docstring |
| Suite integrity | Full offline gate after every task; Task 0 collect-count baseline |

## Risks

1. **Task 3 is the only behavioral change** — tie case only, nothing else exercises it (checked: only test_entity_resolution tests `_normalized_name`'s guard, not `_by_entity_alias`'s). If anything else goes red: downgrade to xfail + record (STOP rule a).
2. **Fuzzy-arm arithmetic** — the 0.9 boundary constructions are fiddly; the sanity-assert pattern makes any mis-derived case fail loudly and self-explainingly rather than pinning the wrong contract.
3. **Concurrent session** — TODO.md/config may change under us mid-wave; stage by explicit path only, re-check status before every commit, never touch BUILD_NOTES.md/config/*.
4. **Wikipedia guard** could interact with the one real-fetch end-to-end test (test_discover_waterfall.py:457-484) — that test uses Wikidata, not Wikipedia; if red anyway, STOP rule (b).

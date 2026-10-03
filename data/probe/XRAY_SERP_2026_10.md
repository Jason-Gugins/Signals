# X-ray SERP Probe — Findings (Task 3, 2026-10)

**Date:** 2026-10-02 · **Script:** `scripts/probe_xray_serp.py` (re-runnable, self-paced; PACE_S=4.0 s sleep between ANY two consecutive requests, global) · **Plan:** `.zcode/plans/2026-10-02_182833-xray-serp-prospecting.md` Task 3
**Machine context:** Windows 10 (win32 10.0.19044 x64), git-bash, venv-only run as `.venv\Scripts\python.exe` from repo root. Installed: httpx 0.27.2, curl_cffi 0.16.2, and the native `signals_antibot` Rust engine IS built and importable on this machine.
**UAs:** plain httpx tier carries the honest repo UA (`SignalsResearchBot/0.1 (+contact: …)` from `SIGNALS_CONTACT_EMAIL`, `probe_wikidata_resolve.py` precedent); curl_cffi tier carries the production Chrome/147 UA (the exact factory `src/identity/ddg_ids.py:_default_fetcher` uses); `SignalsTransport` keeps its own Chrome/151 default posture.
**Run:** 15 requests total (budget 24), every consecutive pair ≥4 s apart, zero flake retries. **Verdict: STOP CONDITION TRIGGERED — no transport retrieves the five operator-query SERPs keylessly from this machine.**

## Escalation rung (Task 3b, 2026-10-02)

Same day, same machine, same script (`scripts/probe_xray_serp.py --escalation`, follow-ups via `--lite-xcheck [qid …]`). The plan's named escalation (browser tier / solve-and-bounce) plus two cheap variants, per the plan Task 3 "Expected outcomes" line. Escalation budgets held: **2 of 6 browser page loads, 7 of 8 added HTTP requests** (the per-invocation budget ledger resets between script runs; totals below are the true cross-invocation sum). Every consecutive request within a run ≥4 s apart; between script invocations the gap was operator-paced (minutes). **No hard block ever retried**; every body classified by markers/anchors, never status. One classifier change for this rung: ≥3 external anchors is trusted as organic FIRST (block pages link only to their own properties, which the anchor counter filters out), so the soft `/httpservice/retry/enablejs` marker cannot mask a page whose gate has resolved; hard markers (`/sorry/`, `unusual traffic`, `g-recaptcha`, `recaptcha`, `consent.google.com`, `unfortunately, bots use duckduckgo`, `anomaly-detected`, `captcha`) still win whenever anchors are below 3.

### Escalation matrix (engine × path; body-validated)

| Engine | Path | Verdict | Evidence |
|---|---|---|---|
| google | E1 headed Patchright browser (`headless=False` per ghost.py; real JS execution, 25 s DOM settle) | **BLOCK** | 751 KB DOM, title = query, **zero result anchors after 25 s of executed JS**. This is the modern JS-gate: a full app shell (search-box UI rendered) whose `<noscript>` fallback meta-refreshes to `/httpservice/retry/enablejs`; its own inline JS watches responses for `X-Sorry-Redirect` / `/sorry/index` (present as JS strings only — no /sorry/ or captcha body ever served). |
| google | E2 cookie-amortized replay (browser cookies AEC, DV, NID, SEARCH_SAMESITE, __Secure-STRP → `CurlCffiFetcher.get(url, cookies=…)`, Chrome/147) | **BLOCK** | 200, 499 KB — the same JS-gate app shell, 0 anchors. |
| ddg | E1 headed browser on `html.duckduckgo.com/html/` (operator q1) | **DEAD** | the 273 B error-lite body — the html endpoint rejects operator queries even for a **genuine headed Chromium**. Client fingerprint is irrelevant; the query shape is filtered endpoint-side. No cookies set (nothing to replay). |
| ddg | E2 cookie replay | SKIPPED | no DDG cookies harvested in E1. |
| ddg | E3 POST form on html endpoint (`q=<operator q1>`, curl_cffi) | **DEAD** | 403, 236 B error-lite. |
| ddg | E3 GET `lite.duckduckgo.com/lite/?q=<operator q1>` (curl_cffi, Chrome/147) | **WORKS** | 200, 15 KB, 4 organic result anchors — real on-target profiles (below). |
| ddg | E3+ lite cross-check — q2 (`site:` family), q5 (`intitle:`), q3 (quote-pair) | **CHALLENGE** | 202 anomaly body ("Unfortunately, bots use DuckDuckGo too") on all three, minutes apart. |
| ddg | E3+ lite plain control (`stripe official website`) | **WORKS** | 200, 22 KB, 10 organic anchors — passed AFTER two 202s, so the gate is not a session-wide velocity wall. |

The four organic q1 results (`tmp/probe_xray_ddg_lite_q1.html`, wrapped `//duckduckgo.com/l/?uddg=`, `result-link` class): *Austin Heaton — Head of Growth | GTM Engineer*, *Austin Grant — Head of Growth @ Chexy*, *Austin Holdsworth — Head of Growth Marketing*, *Tim Austin — Head of Growth @ Embryo* — exactly the `site:linkedin.com/in "head of growth" "Austin"` target.

### What the escalation proved

1. **Google is closed on this machine — browser tier included.** The headed Patchright browser (the repo's G2-proven bypass posture) received the same gate. The rung-1 93 KB shell has a bigger sibling: a query-titled ~500–750 KB app shell carrying result data as inline JS that never renders anchors. `/httpservice/retry/enablejs` remains the only stable block marker; `/sorry/`, captcha and consent bodies were never served on any path.
2. **The browser-solve amortization story is disproven for google.** Replaying the exact query with the browser's own cookies through the TLS tier returns the same anchor-less shell. There is no "browser solve per window, TLS replay between" for google.
3. **DDG html endpoint is operator-hostile client-agnostically** (real headed browser gets error-lite too); POST form does not help. Keep operator queries off `html.duckduckgo.com` entirely.
4. **DDG lite CAN serve operator-query SERPs keylessly** — the first organic operator body captured from this machine — but **unreliably: 1 of 4 operator queries passed in the observed window** (q1 GO; q2/q5/q3 202-challenged), while plain queries pass consistently (2/2 across rungs). The lite anomaly wall is stochastic on operator syntax — not endpoint-wide, not velocity-based (plain passed after two 202s).

### FINAL VERDICT (supersedes rung 1's stop condition)

- `XRAY_DEFAULT_ENGINE`: **ddg** — default transport path: **curl_cffi chrome-TLS (Chrome/147 UA), GET `https://lite.duckduckgo.com/lite/?q=…`**. Pure TLS tier; no browser solve needed, so no amortization story is required for the default path (and the google amortization story is disproven anyway).
- **Reliability caveat, recorded not softened:** lite passed 1/4 operator queries in-window. The runner must body-validate every response, treat the 202 anomaly as challenge (explicit ledger row, never a retry within a run), and expect some operator shapes to fail per run. The per-string ledger (plan Task 6) is the instrument that measures lite's real pass rate over time before any cadence talk.
- Task 4 fixtures: `ddg_serp.html` comes from **`tmp/probe_xray_ddg_lite_q1.html`** — NOTE the lite markup differs from the html endpoint (`result-link` class, `//duckduckgo.com/l/?uddg=` wrap; the parser must unwrap `uddg` and must not assume `result__a`). **`google_serp.html` still cannot be captured** — no organic google body exists from this machine on any path including the browser tier; the only google bodies are JS-gate shells (`tmp/probe_xray_google.html`, `tmp/probe_xray_google_browser.html`, `tmp/probe_xray_google_replay.html`). Task 4's google parser can only be fixture-tested against the challenge shape.
- If lite's live pass rate proves too low at Task 10's smoke run, that is a new probe decision for Jason (reputation/proxy work is out of scope for this build).

### Escalation samples (tmp/, gitignored)

- `tmp/probe_xray_google_browser.html` (751 KB JS-gate app shell, headed browser, 0 anchors) + `tmp/probe_xray_google_browser_cookies.json` (AEC, DV, NID, SEARCH_SAMESITE, __Secure-STRP @ .google.com)
- `tmp/probe_xray_ddg_browser.html` (273 B error-lite, headed browser) + `tmp/probe_xray_ddg_browser_cookies.json` (empty)
- `tmp/probe_xray_google_replay.html` (cookie-amortized replay — same JS-gate shell, 499 KB, 0 anchors)
- `tmp/probe_xray_ddg_lite_q1.html` — **the organic operator SERP (GO evidence; Task 4 fixture source)**
- `tmp/probe_xray_ddg_lite_{q2,q5,q3}.html` — 202 anomaly bodies; `tmp/probe_xray_ddg_lite_ctrl_plain.html` — plain control organic (10 anchors)
- `tmp/probe_xray_ddg_post.html` — html endpoint POST form: 403 error-lite

### Escalation budget disclosure

| Host | browser loads | HTTP requests | total |
|---|---|---|---|
| www.google.com | 1 | 1 (cookie replay) | 2 |
| html.duckduckgo.com | 1 | 1 (POST form) | 2 |
| lite.duckduckgo.com | — | 5 (q1, q5, q3, q2, plain ctrl) | 5 |

Escalation totals: **2/6 page loads, 7/8 HTTP**. No challenge body was ever re-requested — zero requests in the whole record were retries.

## Verdict matrix (body-validated, never status-validated)

| Engine | Transport | Verdict | Evidence (probe query 1 of 5) |
|---|---|---|---|
| google | httpx (honest UA) | **DEAD** | 200, 92.5 KB, JS-gate shell — 0 external anchors, no classic challenge marker |
| google | curl_cffi chrome-TLS | **DEAD** | 200, 92.9 KB, same JS-gate shell |
| google | signals_transport (native engine live) | **DEAD** | 200, 93.2 KB, same JS-gate shell |
| ddg | httpx (honest UA) | **CHALLENGE** | 202, 14.3 KB — "Unfortunately, bots use DuckDuckGo too." |
| ddg | curl_cffi chrome-TLS | **CHALLENGE/DEAD** (all 5 operator queries fail; per-query detail below) | site:-queries → 403 error-lite (236 B); quote-only queries → 202 challenge body |
| ddg | signals_transport | **DEAD** | native engine attempt fell back to curlcffi (`via=tier1_curlcffi_fallback`) → same 403 as curl_cffi |

Verdicts are per the five plan operator queries — that IS the method; per-query evidence below. Both engines served their blocks with HTTP 200 or 403/202 alike, so every cell was classified from the body, never the status.

## Google: the JS gate (a new block shape — no /sorry/, no captcha)

All three transports received the same-shaped response: **HTTP 200, ~92–93 KB, title "Google Search", ZERO organic anchors**. The only outbound hrefs are `/httpservice/retry/enablejs?sei=…` plus a support link — Google's **JavaScript-required interstitial**, not a results page.

- None of the planned google challenge markers (`/sorry/`, `unusual traffic`, `g-recaptcha`, `recaptcha`, `consent.google.com`) appear — the block happens **before** the captcha stage.
- Query-independent: a plain control query (`stripe official website`) got the identical shell — this is a client-fingerprint gate, not per-query reputation.
- Even the native Rust engine (real Chrome TLS + byte-exact h2, `via=tier1_native` confirmed on the google request) got the same shell.
- Consistent across httpx / curl_cffi / native: all three fired within minutes of each other, all 200/shell.

**For Task 4's detection list: add `/httpservice/retry/enablejs` as a Google JS-gate marker** — a 200 body carrying this href and zero organic anchors is a block page, never a parseable SERP.

## DDG: the operator syntax is the trigger (plain queries still GO)

The September verdict (`KEYLESS_IDENTITY_2026_09.md` rung 5: curl tier 200 GO) **reproduced for plain queries** but **not for operator queries**. Same transport, same session window, paced seconds apart — the query shape is the discriminator:

| Query | Transport | Status | Class / evidence |
|---|---|---|---|
| `site:linkedin.com/in "head of growth" "Austin"` (q1) | curl_cffi | 403 | DEAD — 236 B error-lite page ("email error-lite+…@duckduckgo.com") |
| `site:linkedin.com/in "we only hire senior"` (q2) | curl_cffi | 403 | DEAD — same error-lite |
| `"we're hiring" "head of sales"` (q3) | curl_cffi | 202 | CHALLENGE — "Unfortunately, bots use DuckDuckGo too." |
| `"recently funded" "compliance software"` (q4) | curl_cffi | 202 | CHALLENGE |
| `intitle:"head of growth" site:linkedin.com/in` (q5) | curl_cffi | 202 | CHALLENGE |
| `stripe "official website"` (single-quote control) | curl_cffi | 200 | DEAD — genuine "no results" page (9.3 KB, no `result__a`) |
| `stripe official website` (plain control) | curl_cffi | 200 | **WORKS** — 28.7 KB, 9 `result__a` anchors / 9 external URLs |

Failure taxonomy observed: `site:`/`intitle:` operators → server **403 error-lite**; quoted-phrase pairs → **202 anomaly challenge**; plain queries → full organic SERP. httpx on the operator query got the 202 challenge as before (plain tier stays dead). `signals_transport` on DDG attempted the native engine, fell back to curlcffi, and inherited the same 403 — the native tier adds nothing here.

The tier is alive; DDG's html endpoint is filtering **operator-syntax queries** from keyless clients. Whether rising IP/UA reputation is a co-factor cannot be ruled out, but a plain query fetched fine in the same window, so the query shape is the proximate trigger.

## STOP CONDITION — what this means for the plan

Plan Task 3 expected outcomes: WORKS → proceed; CHALLENGE on both → DDG default + solve-and-bounce escalation; **ALL DEAD → stop, bring findings to Jason, do not force the build.**

**Observed: all five operator queries fail on BOTH engines across ALL THREE transports (DEAD/CHALLENGE). The stop condition triggered. No fetch code is wired and no runner default is pinned.**

- `XRAY_DEFAULT_ENGINE`: **none** (no engine served an operator-query SERP)
- Transport factory for Task 7: **none pinned** — there is no probe-verified transport for the method's queries.

What could change the verdict (untested — outside this probe's matrix; Jason's call before any build):

1. **Browser tier (Patchright / solve-and-bounce, the G2 pattern)**: executes the google JS gate and the DDG anomaly wall. Untested here — the matrix was transport-tier only. This is the plan's open question 1.
2. **DDG session-posture variations** for operator queries (cookie warm-up from a plain query, POST form, `lite.duckduckgo.com`): untested; any such variation is a NEW probe rung, not an assumption to build on.
3. Still-proven-live capability: `ddg/curl_cffi` **plain-name** queries — the existing `DdgSerpResolver` discovery path is unaffected by this probe.

## Challenge / block markers observed (Task 4 detection list, if the build proceeds)

| Marker | Source | Body evidence |
|---|---|---|
| `unfortunately, bots use duckduckgo` | DDG anomaly page | 202 + 14.3 KB body (httpx and curl_cffi) |
| `error-lite+…@duckduckgo.com` mailto | DDG hard 403 | 236 B bare error page, HTTP 403 (curl_cffi) |
| `/httpservice/retry/enablejs` (JS gate) | Google | 200 + ~93 KB shell, zero anchors (all transports) |
| `/sorry/`, `unusual traffic`, `g-recaptcha`, `recaptcha`, `consent.google.com` | — | **NOT observed** — google now blocks before the captcha stage |

DDG classification used the exact marker list from `src/identity/ddg_ids.py:63-68` (`unfortunately, bots use duckduckgo`, `anomaly-detected`, `anomaly`, `captcha` — aligned, most-specific-first, per the probe brief).

## Raw artifacts (tmp/ — gitignored, NOT committed; Task 4's fixtures would come from them if/when the build proceeds)

- `tmp/probe_xray_google.html` — google JS-gate shell (curl_cffi, operator q1) — also saved as `tmp/probe_xray_google_dead.html` by a re-run
- `tmp/probe_xray_diag_google_plain.html` — plain-query control, identical JS-gate shell
- `tmp/probe_xray_ddg_challenge.html` — DDG 202 anomaly body (httpx, operator q1)
- `tmp/probe_xray_ddg_403_diag.html` — DDG 403 error-lite body (curl_cffi, operator q1)
- `tmp/probe_xray_ddg.html` (copy of `probe_xray_diag_ddg_plain.html`) — the only organic WORKS body captured: a **plain-query** DDG SERP with 9 `result__a` anchors, NOT an operator query
- `tmp/probe_xray_diag_ddg_{op_q3,q2_niche,q4_funded,q5_intitle,ctrl_quoted}.html` — per-query evidence bodies

Caveat recorded for the plan: **google has NO organic body** — Task 4's `google_serp.html` fixture cannot be captured from this machine today; only the JS-gate block shape exists as evidence.

## Per-host request budget disclosure

Limit: ≤24 total network requests for the whole run (probe brief); PACE_S=4.0 s between ANY two consecutive requests (satisfies the ≥3 s same-host rule globally). No retries of challenge responses anywhere.

| Host | scripted matrix run | diagnostics | total |
|---|---|---|---|
| www.google.com | 3 | 2 | **5** |
| html.duckduckgo.com | 3 | 7 | **10** |

Total: **15 of 24**. Diagnostics were three paced follow-up batches after the scripted matrix run: body inspection of the unexplained google 200/DEAD and ddg 403, then the query-shape controls that isolated the operator-syntax trigger.

## Live smoke run (Task 10, 2026-10-02/03)

Command: `.venv\Scripts\python.exe -m src.cli xray --kind hiring --role "head of sales" --limit 1 --pace 8 --attempt-pause 30`

**VERDICT: GO on the first fetch.** One query, status `ok`, attempts 1, zero challenges.

- Built query (from `config/lists/xray_strings.yaml:hiring_post_role`): `"head of sales" "we're hiring" -"jobs" -"preferred"` — role slot filled, defaults.exclude minus-group applied.
- ddg_lite fetch → clean body → 1 organic result → company hit `useshiny.com` ("Head of Sales: The Practical Guide for Growing Companies") queued into `identity_candidates` (kind=domain, source=xray, status=pending). Ledger row `data/xray/ledger.jsonl` carries full provenance (string_id, query, attempts, counts, UTC stamp).
- Yield note: 1 result for a single query is thin — the lite endpoint returns few organic results per operator query and the stochastic gate filters some. Operators should run several strings/kinds per session and read `--stats` over time; this does not change the GO verdict (the chain is proven end-to-end), but per-string yield tracking is exactly what the ledger exists for.

## Shell re-analysis (bypass ladder Task 1, 2026-10-03)

**ZERO network requests** — offline re-analysis of the captures already on disk (`tmp/`, gitignored) with a new throwaway analyzer, `scripts/analyze_google_shell.py` (plain stdlib; re-runnable as `.venv\Scripts\python.exe scripts\analyze_google_shell.py <file.html> …`; no args = the known XRAY google set). Purpose: the escalation rung concluded "JS-gate holds headed" from ZERO *anchor* matches — but modern Google SERPs hydrate late and embed result data as inline JS state, and `/httpservice/retry/enablejs` lives in the standard `<noscript>` block present on EVERY Google page. This section settles what the captures actually contain.

**VERDICT: `RESULTS_AS_DATA`.** Both the headed-Patchright browser capture and the browser-cookie replay capture contain a full organic result set — 9 rendered `h3` result tiles in the DOM **plus 12 plaintext result records (url + title) in the embedded JS state**. The prior "zero result anchors" and "same shell" conclusions for google were a **measurement artifact**, not a wall. Per the plan's decision matrix: Leg A becomes a **parsing task** (engine `google_state`) — skip to Task 6. No overlay was involved (marker (g) all zeros, see table), so `CONSENT/CHALLENGE_OVERLAY` and `TRULY_EMPTY` are both ruled out.

### Per-capture marker table (analyzer output, verified by manual greps)

| Marker | `browser.html` 751,687 B (headed Patchright) | `replay.html` 499,539 B (cookie replay) | `google.html` 93,205 B (curl shell) | `diag_google_plain.html` 92,988 B (curl shell) |
|---|---|---|---|---|
| (a) `<title>` | the operator query | the operator query | "Google Search" | "Google Search" |
| (b) `h3` elements | **9** | **9** | 0 | 0 |
| (c) external http(s) hrefs (non-google.com) | 1 (google.ca products footer) | 1 (same) | 0 | 0 |
| (d) `AF_initDataCallback` blocks / bytes | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| (d) `window.google` mentions / `<script>` bytes containing it | 24 / ~34.7 KB | 23 / ~36.9 KB | 6 / ~27.7 KB | 6 / ~27.5 KB |
| (d) `W_jd` hits (window state blob, case-sensitive; 22 case-insensitive) | 21 | 21 | 0 | 0 |
| (e) `id="search"` (exact quoted) | 1 | 1 | 0 | 0 |
| (e) `data-ved` attrs | **304** | **212** | 0 | 0 |
| (f) `linkedin.com/in` anywhere in raw bytes | **104** | **91** | 1 (query echo, see below) | 0 |
| (f) `/url?q=` / `uddg` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| (f) distinct `linkedin.com/in/<slug>` paths in raw bytes | **12** | **12** | 0 | 0 |
| (g) `g-recaptcha` / `consent.google.com` / `unusual traffic` / `recaptcha` | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| (g) `/sorry/` + `X-Sorry-Redirect` (JS strings only — never served) | 5 + 4 | 5 + 4 | 0 + 0 | 0 + 0 |
| (g) `httpservice/retry/enablejs` / `<noscript>` | 2 / 1 | 2 / 1 | 2 / 1 | 2 / 1 |
| result-tile class `MjjYud` | 20 | 20 | 0 | 0 |
| `"eem"` marker (plan hypothesis) | 0 | 0 | — | — |
| `"errupt"` marker (plan hypothesis) | 1 — substring of `"non-interruptible SRP"` in the SearchGuard JS, not a result marker | 1 (same) | — | — |
| `"2003"` url+title state records (see evidence) | **12** | **12** | 0 | 0 |
| cookies json (`browser_cookies.json`, informational) | JSON array of 5 cookies (AEC, DV, NID, SEARCH_SAMESITE, __Secure-STRP posture) — no result content, as expected | | | |

Analyzer note: its loose `id="search"` regex also matches `id="searchform"` (reports 2 on the big captures); the exact quoted `id="search"` count is 1.

### Why the old probe saw "zero result anchors" — the measurement artifact

1. **The result links are RELATIVE opaque redirects, not external hrefs.** Each of the 9 tiles is `<a href="/goto?url=CAES…">` wrapping `<h3 class="LC20lb MBeuO DKV0Md">…</h3>`. An "external http(s) anchor" counter sees **zero** — every result anchor is a relative `/goto` href whose `url=` param is an opaque base64 blob (base64-decoding yields binary garbage — encrypted/protobuf, NOT the plaintext target). The old counter classified the page by exactly that signal.
2. **`linkedin.com/in` hits hid in plain sight.** The first raw hit in every capture is the `<title>` echoing the query (`site:linkedin.com/in "head of growth" "Austin"`); in the 93 KB curl shells the ONLY hit is the query echoed inside the hidden "If you're having trouble accessing Google Search…" retry link (`/search?q=site:linkedin.com/in+…&emsg=SG_REL`) — the JS-gate fingerprint. But the big captures additionally carry **12 distinct `linkedin.com/in/<slug>` paths** as result data.
3. **`/httpservice/retry/enablejs` is NOT a block discriminator.** It appears exactly twice, inside the single standard `<noscript>` block, on ALL FOUR captures — including the two that carry full results. A 200 body can carry the enablejs noscript AND a complete result set. The escalation rung's soft-marker-first classifier was right to distrust it; the anchor count was the wrong replacement signal.
4. **The `/sorry/` + `X-Sorry-Redirect` strings are guard JS, not a challenge.** Context confirmed by eye: they live inside the app-shell's own response-watching code ("…forced reload of a non-interruptible SRP…", iframe logic that would display `/sorry/index` IF a response said so). No /sorry/ or captcha body was ever served — consistent with the original record; and irrelevant, because the page has results.

### Extracted evidence — result records as embedded state (verbatim strings)

The state blob is per-tile `window.W_jd`-style data (keyed by tile ids like `az_AapiWNJbDruEPo8upyAE7`) containing `"2003":[null,"<token>","<url>","<title>",…]` records — plaintext url + title pairs. Both big captures carry the IDENTICAL 12 records. First 5, fully:

1. `https://www.linkedin.com/in/tylerdurman` — `Tyler Durman - Who owns the work when growth stalls? | I …`
2. `https://www.linkedin.com/in/austinheaton` — `Austin Heaton - Head of Growth | GTM Engineer | AEO | AI …`
3. `https://www.linkedin.com/in/prasad-bharti` — `Bharti Prasad - Head of Growth |10+ Years Scaling Consumer …`
4. `https://www.linkedin.com/in/james-tice-124911140` — `James Tice - Head of Growth | LinkedIn`
5. `https://www.linkedin.com/in/nickchristensen1` — `Nick Christensen - Head of Growth @ AppSumo • $7M → $90M in …`

Remaining 7: `…/in/whoisaustinwilson` (Austin Wilson - Head of Growth @ Linkt AI), `…/in/sethberman` (Seth Berman - Head of Growth Marketing at Stripe), `…/in/austinwilliamward` (Austin Ward - Head of Growth @ Fathom), `…/in/kyle-rohrmann` (Kyle Rohrmann - Head of Growth, Modern Wisdom), `…/in/maxbibeau` (Maxwell Bibeau - Head of Growth at Variational), `https://uk.linkedin.com/in/tim-austin-230b8a169` (Tim Austin - Head of Growth @ Embryo), `…/in/matthewswan15` (Matt Swan - Head of Growth at The Scalable Company LLC).

The 9 rendered `h3` titles (DOM, class `LC20lb MBeuO DKV0Md`), verified by eye on the raw excerpt: Austin Grant - Head of Growth @ Chexy | GTM & Partnerships · Austin Ward - Head of Growth @ Fathom | Stanford MBA + … · James Tice - Head of Growth · Austin Heaton - Head of Growth | GTM Engineer | AEO · Cory Barbot - Head of Growth · Patryk Włodarski - Head of Growth Marketing @ Arrived · Nick Christensen - Head of Growth @ AppSumo · Mac Austin - Growth & Performance Marketing Strategist · Asad Kanaan - Head of Growth / VP Marketing. `<cite>`s on the person tiles carry follower counts ("3.9K+ followers"). All on-target for `site:linkedin.com/in "head of growth" "Austin"`.

Manual verification (step 3 of the task) — analyzer numbers cross-checked by eye with independent greps on both big captures: `data-ved` 304/212, `<h3` 9/9, `MjjYud` 20/20, `linkedin.com/in` 104/91, `W_jd` 21 (22 case-insensitive), `id="search"` 1 — plus three representative raw excerpts inspected directly: (1) the first h3 tile (tracking href + `LC20lb` title + favicon data-URI), (2) the first `W_jd` window-state assignment (`if(window.W_jd)for(var b in a)window.W_jd[b]=a[b];else window.W_jd=a;` followed by `WIZ_global_data`), (3) a `"2003"` record showing `"https://www.linkedin.com/in/austinheaton","Austin Heaton - Head of Growth | GTM Engineer | AEO | AI …"` verbatim. Analyzer and greps agree.

### What this changes

- **Escalation rung corrections (supersede the E1/E2 BLOCK rows and "What the escalation proved" §1–2 above):** the headed browser did NOT hit a JS gate — it received a full SERP whose links the anchor counter could not see. The cookie replay did NOT return "the same anchor-less shell" — it returned the same 12-result SERP. Amortization (browser solve once → TLS replay with its cookies) is therefore **back on the table** for google, untested-but-plausible; the honest statement is that BOTH fetch paths provably return parseable result bodies.
- **Task 6 shape (`google_state` engine):** fetch = existing browser tier OR browser-cookie replay through `CurlCffiFetcher` (both proven above); parse = (i) regex the `"2003":[null,"<tok>","<url>","<title>",…]` records from the raw bytes — zero DOM dependency, gives full plaintext URLs — and/or (ii) DOM-extract `h3.LC20lb` tiles for titles + cites (the `/goto?url=` href itself is opaque and yields no URL). Fixture source: `tmp/probe_xray_google_browser.html` (and `_replay.html`) — no re-fetch needed to build fixtures.
- **Marker list update:** `id="search"` + `data-ved` + `h3` + `"2003":\[` are the google RESULTS markers; `httpservice/retry/enablejs` alone must never classify a body as blocked (it is present on result pages too).

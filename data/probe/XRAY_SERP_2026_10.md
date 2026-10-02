# X-ray SERP Probe — Findings (Task 3, 2026-10)

**Date:** 2026-10-02 · **Script:** `scripts/probe_xray_serp.py` (re-runnable, self-paced; PACE_S=4.0 s sleep between ANY two consecutive requests, global) · **Plan:** `.zcode/plans/2026-10-02_182833-xray-serp-prospecting.md` Task 3
**Machine context:** Windows 10 (win32 10.0.19044 x64), git-bash, venv-only run as `.venv\Scripts\python.exe` from repo root. Installed: httpx 0.27.2, curl_cffi 0.16.2, and the native `signals_antibot` Rust engine IS built and importable on this machine.
**UAs:** plain httpx tier carries the honest repo UA (`SignalsResearchBot/0.1 (+contact: …)` from `SIGNALS_CONTACT_EMAIL`, `probe_wikidata_resolve.py` precedent); curl_cffi tier carries the production Chrome/147 UA (the exact factory `src/identity/ddg_ids.py:_default_fetcher` uses); `SignalsTransport` keeps its own Chrome/151 default posture.
**Run:** 15 requests total (budget 24), every consecutive pair ≥4 s apart, zero flake retries. **Verdict: STOP CONDITION TRIGGERED — no transport retrieves the five operator-query SERPs keylessly from this machine.**

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

# G2 Competitors/Alternatives Probe — Findings (T5, 2026-09)

**Date:** 2026-09-05 · **Script:** `scripts/probe_g2_competitors.py` (paced, PACE_S=5.0 s between ANY two requests global, hard budget: ≤3 G2 fetches for the whole run) · **Plan:** `.zcode/plans/2026-09-05_205645-keyless-clay-exa-clones.md` Task T5
**Target:** `https://www.g2.com/products/slack/competitors/alternatives` ("Top 10 Slack Alternatives & Competitors" — URL shape per prior external review; plain server-side GET known 403/DataDome)
**Tiers:** plain rung = `CurlCffiFetcher` `impersonate="chrome"` with the Chrome/147 UA the production curl tier ships (`src/core/curl_fetcher.py`, `capterra_resolve._default_fetcher` precedent). Browser rung = the exact `probe_g2_reviews.py` machinery (patchright chromium **headed**, config browser context 1920×1080 / en-US / America/Toronto / Chrome-147 UA, `data/g2_cookies.json` session cookies injected: `cf_clearance`, `_g2_session_id`, `__cf_bm` — no `datadome` cookie in the jar).
**Run:** 1 G2 fetch of budget 3, zero flake retries (KNOWN FLAKE RULE never triggered). Verdicts are body-validated, never status-validated.

## Verdict table

| # | Rung | URL | G2 fetches | Status | Verdict | Gates |
|---|------|-----|-----------|--------|---------|-------|
| 1 | plain (curl_cffi chrome) | slack `/competitors/alternatives` | 1 | 403 | **CHALLENGE-BLOCKED** — DataDome interstitial body | T6 |
| 2 | browser (patchright headed) | slack `/competitors/alternatives` | 0 (never reached G2) | n/a | **NO-GO-for-now — browser launcher error** (environment, not anti-bot) | T6 |
| 3 | parse-surface (offline) | — | 0 | — | **NOT REACHED** — no rung returned real markup | T6 |
| 4 | optional 2nd target (hubspot-marketing-hub) | — | 0 (correctly skipped — slack capture did not succeed) | — | SKIPPED by budget guard | T6 |

**Overall: NO-GO-for-now.** The curl tier is dead on this page (proven live). The browser tier is currently blocked by a *local* defect with a one-command fix — DataDome's behavior against the browser tier on this URL is **untested**, not failed.

---

## 1. Plain rung — curl_cffi `impersonate="chrome"` → CHALLENGE-BLOCKED

**Request (1):** `GET https://www.g2.com/products/slack/competitors/alternatives` → **HTTP 403**, 1,707 bytes, no redirect.

Body is a genuine DataDome interstitial, not an HTTP-only rejection:

```html
<html lang="en"><head><title>g2.com</title>...</head>
<body style="margin:0"><p id="cmsg">Please enable JS and disable any ad blocker</p>
<script data-cfasync="false">var dd={'rt':'i','cid':'AHrlqAAAAAMA80CgA7p5vEcA-xeMFg==',
'host':'geo.captcha-delivery.com','cookie':'jCH9ANat0T0TFVVwakj~gnVcaK7eGg6dPUxev_h7qiohLVT2CyUFo2P77qFnVravw...'</script>
```

Marker scan (case-insensitive): `geo.captcha-delivery.com`, `captcha-delivery.com`, `captcha` all present; real-markup markers `product-card` / `link-product-card` = **0**, `/products/<slug>` hrefs = **0**. This reproduces the prior external-review finding (plain server-side GET → 403 DataDome) against the exact TLS fingerprint the production T6 curl tier would use — so it is not a UA/fingerprint mismatch artifact.

**Verdict: NO-GO for the curl tier.** Do not build the T6 parser on plain-fetch HTML for this page.

## 2. Browser rung — patchright (machinery of `probe_g2_reviews.py`) → NO-GO-for-now (launcher error)

The rung never issued a G2 request: `pw.chromium.launch(headless=False, ...)` failed before navigation:

```
BrowserType.launch: Executable doesn't exist at
C:\Users\Jason\AppData\Local\ms-playwright\chromium-1161\chrome-win\chrome.exe
```

Root cause (environment, recorded verbatim in `g2_competitors_findings.json`): the installed **patchright 1.51.3** pins **chromium-1161**, but `ms-playwright` on this machine holds **chromium-1140** and **chromium-1234** only — a revision mismatch, not an anti-bot block. The launcher error consumed 0 of the G2 budget; per the T5 brief it is recorded as the verdict evidence with no retries.

Fix (one command, then re-probe): `./.venv/Scripts/python.exe -m patchright install chromium`, then rerun `scripts/probe_g2_competitors.py` (a fresh run is a new probe with its own 3-fetch budget). Prior runs of the same machinery on this machine cleared DataDome on G2 review pages (`data/probe/g2_databricks_rendered.html`, `g2_databricks_probe.json`), so the browser tier is the plausible path — but that is history, not a T5 result: **this probe makes no claim about whether DataDome clears on `/competitors/alternatives`.**

## 3. Parse-surface — NOT REACHED (the open question for T6)

No rung returned real markup, so the offline parse-surface analysis had no input. Still unknown, and exactly what the T6 parser depends on:

1. **Server-rendered anchors** `a[href="/products/<slug>"]` inside `link-product-card`/`product-card` containers (count + first-10 slugs/names from link text) — the rung the script is wired to measure.
2. **Embedded JSON** — `script#__NEXT_DATA__` / `self.__next_f` fragments / `elv-*` classes (the reviews page ships `elv-*`; whether the competitors page ships a parsed JSON payload with the competitor list is unverified).
3. **Nothing parseable** — would force a DOM-wait extraction path.

The script implements all three checks and reports the exact selector/fragment path plus a 600-char card-container snippet for T6; it simply needs real markup to run on.

## 4. Optional second target — SKIPPED (budget guard worked)

`hubspot-marketing-hub/competitors/alternatives` was gated on "slack capture succeeded AND budget remains". Slack did not succeed, so the guard correctly spent nothing — the URL-shape generalization question remains open with it.

## Budget disclosure

Limit: ≤3 G2 fetches for the whole run, hard-capped in `Budget` (no code path can issue a 4th); ≥5.0 s between ANY two requests global (`Pacer`).

| Host | plain | browser | parse-surface | 2nd target | total |
|------|-------|---------|---------------|------------|-------|
| www.g2.com | 1 | 0 | 0 | 0 | **1 / 3** |

Zero flake retries; zero requests to any other host. The rung driver refused nothing else — the remaining 2 fetches were unspendable by design (browser rung never launched; 2nd target gated on slack success).

## What this means for T6

- **Verdict: NO-GO-for-now — do not start the T6 build yet.** Two blockers, one proven and one environmental:
  1. **The plan's curl-tier assumption is dead for this page.** T6 as planned ("curl tier for G2 pages, capterra-style internal `CurlCffiFetcher`") would 403 on every fetch — proven live with the production fingerprint. The plan line needs amending to the browser tier.
  2. **The browser tier is launcher-blocked locally** (patchright 1.51.3 ↔ chromium-1161 missing; `patchright install chromium` fixes it). Until the rerun clears DataDome and captures real markup, the parse-surface answer T6 needs does not exist — do not write the parser against guessed selectors.
- **Recommended cadence: manual script** (unchanged from the plan) — one paced `scripts/probe_g2_competitors.py`-style run per onboarding batch, never a cadence adapter; DataDome escalates under repeated probing, hence the ≤3-fetch run budget and 5 s pacing carrying into any T6 fetcher.
- **Expected yield: unproven.** If the rerun clears, expectation based on the G2 reviews-page precedent is a server-rendered page exposing ~10 competitor cards with `/products/<slug>` hrefs and product names in link text (the exact "Top 10 Alternatives" list T6 wants, plus rating snippets), capped per account and written to `identity_candidates` (kind=competitor) with human review per the plan.
- **Next step:** `patchright install chromium` → rerun probe → fill the parse-surface section of this file's successor → then T6 GO/NO-GO is decidable on evidence.

## Rerun (same day, after `patchright install chromium`) — browser tier CHALLENGE-BLOCKED

Environment fixed (chromium-1161 headless shell downloaded), probe rerun with a fresh 3-fetch budget:

| # | Rung | G2 fetches | Status | Verdict |
|---|------|-----------|--------|---------|
| 1 | plain (curl_cffi chrome) | 1 | 403, 1,707 bytes | **CHALLENGE-BLOCKED** (DataDome interstitial, identical to run 1) |
| 2 | browser (patchright headed, stale `data/g2_cookies.json`) | 1 | page loaded, 2,580 bytes | **CHALLENGE-BLOCKED** — DataDome challenge body, `geo.captcha-delivery.com` markers, 0 real-markup markers |

**Overall after two runs: NO-GO.** The browser tier reached G2 and was still served a DataDome challenge — the stored session cookies are stale (the jar holds `cf_clearance`/`_g2_session_id`/`__cf_bm` but no fresh `datadome` cookie). Per the anti-bot escalation discipline (stop after 2–3 captures; never hammer a gated host), probing STOPS here: 3 G2 fetches total across both runs.

**Paths to a real capture (any one unblocks T6):** (a) the human's real browser exports a fresh `datadome` cookie into `data/g2_cookies.json`; (b) the optional DataDome solver keys (`DATADOME_SOLVER_API_KEY` / `DATADOME_RESIDENTIAL_PROXY`) are configured and the marketplace bypass waterfall runs once. Until then the parse-surface answer does not exist and the T6 parser must not be written against guessed selectors.

**Re-scope decision (plan deviation, recorded):** T6's G2 competitor-discovery pass is DEFERRED pending a real capture; its mechanism (write ranked competitor candidates into `identity_candidates` kind=competitor, capped per account, human-gated) moves into T7's keyless mining pass (Bing News RSS + HN Algolia), which has no gating dependency. T8 (list activation + doctor WARN) proceeds after T7.

## Raw artifacts

- `data/probe/g2_competitors_slack_plain.html` — plain-tier DataDome interstitial body (403, 1,707 bytes)
- `data/probe/g2_competitors_findings.json` — machine-readable per-rung record incl. launcher error verbatim, budget log
- `data/probe/g2_competitors_slack.html` — *not written* (browser rung never captured)
- `data/probe/g2_competitors_hubspot.html` — *not written* (guard skipped)

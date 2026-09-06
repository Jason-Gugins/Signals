# Keyless Identity Discovery Probe — Findings (T1, 2026-09)

**Date:** 2026-09-05 · **Script:** `scripts/probe_wikidata_resolve.py` (paced, PACE_S=5.0 s between ANY two consecutive requests, global) · **Plan:** `.zcode/plans/2026-09-05_205645-keyless-clay-exa-clones.md` Task T1
**UA:** honest repo UA on every httpx request — `SignalsResearchBot/0.1 (+contact: jasongugins4@gmail.com)` (`src/core/http.py` → `config.resolved_user_agent()`, email from `SIGNALS_CONTACT_EMAIL`). The curl_cffi tier keeps the `impersonate="chrome"` UA that ships with that stack (`src/core/curl_fetcher.py` precedent) — a bot UA on a Chrome TLS fingerprint would not test the real T3c stack.
**Run:** 11 requests, 51.1 s, zero flake retries needed (KNOWN FLAKE RULE never triggered). **Two full runs were executed** (a robots-group-display fix between them); every verdict reproduced identically — DDG top-3 ordering shifted slightly between runs but stripe.com was #1 in both.

## Verdict table

| # | Rung | Endpoint(s) | Requests | Status | Verdict | Gates |
|---|------|-------------|----------|--------|---------|-------|
| 0 | robots.txt | wikidata / wikipedia / bing | 3 (1+1+1) | 200 / 200 / 200 | **PARTIAL** — `/w/api.php` DISALLOWED on both Wikimedia hosts; bing `/news/search` allowed | T3a, T7 |
| 1 | Wikidata P856 | `wbsearchentities` + `wbgetclaims` | 2 | 200 / 200 | **GO** — Q7624104 → P856 = `https://stripe.com/` | T3a |
| 2 | Wikipedia extlinks | `list=search` + `prop=extlinks` | 2 | 200 / 200 | **GO** — "Stripe, Inc." extlinks contain `https://stripe.com/` | T3a |
| 3 | GKG (Enterprise KG) | `publicKnowledgeGraphEntities:Search` | 1 | 401 | **PENDING-CREDENTIALS** — `UNAUTHENTICATED` / `CREDENTIALS_MISSING` | T3b |
| 4 | kgsearch (legacy, keyless) | `kgsearch.googleapis.com/v1/entities:search` | 1 | 403 | **GATE-CONFIRMED** — `PERMISSION_DENIED` | T3b |
| 5 | DDG ladder | `html.duckduckgo.com/html/` | 2 (plain + curl) | 202 / 200 | **GO curl tier only** — plain tier challenged; curl tier real markup, stripe.com #1 | T3c |

Pacing: every consecutive request pair in the whole run was ≥5 s apart (script-enforced global pace). Per-host-per-rung budget ≤3 — max observed 2. No host served an anti-bot challenge to any Wikimedia/Google/bing endpoint; the only challenge came from DDG, exactly as planned.

---

## 0. robots.txt status (gates T3a robots wiring + T7 bing news)

| Host | Path checked | Allowed for our UA | Matching lines in the applicable group |
|------|--------------|--------------------|----------------------------------------|
| `www.wikidata.org` | `/w/api.php` | **NO** (`*` group) | `Disallow: /w/` |
| `en.wikipedia.org` | `/w/api.php` | **NO** (`*` group) | `Disallow: /w/` |
| `www.bing.com` | `/news/search` | **YES** (`*` group) | none — no `/news` rule exists in the group; allowed by default |

Both Wikimedia robots files use the 2024 rewrite shape: the `*` group opens with narrow `Allow:` exceptions that do **not** cover the plain API path —
```
User-agent: *
Allow: /w/api.php?action=mobileview&
Allow: /w/load.php?
Allow: /api/rest_v1/?doc
Allow: /w/rest.php/site/v1/sitemap
Disallow: /w/
```
so `/w/api.php` falls to the broader `Disallow: /w/` (longest-match). `urllib.robotparser.can_fetch` (authoritative cross-check) agrees with the manual group eval on all three paths.

**Note:** the probe proceeded with the two API rungs anyway (per the T1 brief) to record reality — both returned 200 with the honest UA. Scoped `robots_allow` overrides exist for exactly this situation (`config/default.yaml` `http.robots_allow`, news.google.com/rss precedent), so T3a/T7 can request a scoped exception without touching the global `respect_robots` switch.

---

## 1. Wikidata wbsearchentities → wbgetclaims P856 (gates T3a — `src/identity/wikidata_ids.py`)

**URLs probed (2):**
- `.../w/api.php?action=wbsearchentities&search=Stripe&language=en&type=item&format=json&limit=5` → **200**, `search[]` with 5 entities
- `.../w/api.php?action=wbgetclaims&entity=Q7624104&property=P856&format=json` → **200**, 1 P856 claim

**Collision evidence (top 5 — label alone is ambiguous):**

| QID | Label | Description | Business? |
|-----|-------|-------------|-----------|
| Q7624104 | Stripe | Irish-American payment technology company | **picked** |
| Q3421342 | stripe | long, narrow band of color, often in alternating sets | no |
| Q127900502 | Stripe | fictional character in the Gremlins franchise | no |
| Q117454382 | Stripe | South African progamer | no |
| Q297115 | Meloidae | family of beetles | no |

4 of 5 top-5 entities are non-business; **three share the label "Stripe"**. The pick must be description-keyed (the probe used a payments-hints heuristic over label+description and picked correctly on the first hit).

**P856 result:** exactly 1 claim → `https://stripe.com/` (the official site, apex + trailing slash).

**Verdict: GO.** Two-call shape (search → claims) verified end-to-end; P856 lands directly on the apex domain.

---

## 2. Wikipedia list=search → prop=extlinks (gates T3a — `src/identity/wikipedia_ids.py`)

**URLs probed (2):**
- `.../w/api.php?action=query&list=search&srsearch=Stripe%2C%20Inc.&format=json&srlimit=3` → **200**, top titles: `Stripe, Inc.`, `OpenRouter`, `Frontier Climate`
- `.../w/api.php?action=query&titles=Stripe%2C%20Inc.&prop=extlinks&ellimit=50&format=json` → **200**, 50 extlinks

**stripe.com presence:** 4 of 50 extlinks are stripe.com —
`https://stripe.com/` (first), `https://stripe.com/blog/atlas-llc`, `https://stripe.com/blog/terminal`, `https://stripe.com/blog/terminal-in-person-payments`.

The **first** extlink of the top search hit is the official site; the rest are blog deep links (candidate ranking must prefer the apex, not just "any stripe.com URL").

**Verdict: GO.** Note the extlink set is not sorted usefully beyond hit 1 — apex-domain filtering + a rank preference is required.

---

## 3. Enterprise Knowledge Graph, unauthenticated (gates T3b — `src/identity/gkg_ids.py`)

**URL probed (1):** `https://enterpriseknowledgegraph.googleapis.com/v1/projects/test-project/locations/global/publicKnowledgeGraphEntities:Search?query=Stripe&types=Organization&languages=en&limit=5` → **401**

**Error body (verbatim shape):**
```json
{"error": {"code": 401, "status": "UNAUTHENTICATED",
  "message": "Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. ...",
  "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "CREDENTIALS_MISSING",
    "domain": "googleapis.com",
    "metadata": {"method": "google.cloud.enterpriseknowledgegraph.v1.EnterpriseKnowledgeGraphService.SearchPublicKg",
                 "service": "enterpriseknowledgegraph.googleapis.com"}}]}}
```

The endpoint exists and answers (no 404/HTML gate); the error metadata even confirms the method name `EnterpriseKnowledgeGraphService.SearchPublicKg`. No credentials exist on this machine (`GOOGLE_APPLICATION_CREDENTIALS` unset), so the **entity-level response schema is still uncaptured** — the JSON-LD payload pinning the website-url path must happen on the first credentialed run.

**Verdict: PENDING-CREDENTIALS** (as planned — this rung documents the unauthenticated shape).

---

## 4. Legacy kgsearch, keyless (gates T3b)

**URL probed (1):** `https://kgsearch.googleapis.com/v1/entities:search?query=Stripe&types=Organization&languages=en&limit=1` → **403**

**Error body (verbatim):**
```json
{"error": {"code": 403,
  "message": "Method doesn't allow unregistered callers (callers without established identity). Please use API Key or other form of API consumer identity to call this API.",
  "status": "PERMISSION_DENIED"}}
```

Exactly the planned key gate. With a valid key this endpoint returns `itemListElement[].result.url` (the official site) — the legacy fallback backend stays config-selected **off by default**.

**Verdict: GATE-CONFIRMED** (T3b credential gate proven on both GKG backends).

---

## 5. DDG html SERP ladder for "stripe inc official website" (gates T3c — `src/identity/ddg_ids.py`)

**URL probed (2, one per tier):** `https://html.duckduckgo.com/html/?q=stripe+inc+official+website`

### (a) PLAIN tier — httpx with the honest UA → NO-GO
**HTTP 202**, 14.2 KB, challenge body — marker hit (case-insensitive scan): **"Unfortunately, bots use DuckDuckGo too."** The plain tier is dead, as the plan predicted; do not build it.

### (b) CURL_CFFI tier — `impersonate="chrome"` → GO
**HTTP 200**, 31.0 KB — but the verdict is **body-validated, never status-validated** (the plan's warning: DDG ships challenge pages with 200/202). Body checks:
- 0 challenge markers ("Unfortunately, bots use DuckDuckGo" / "anomaly-detected" / "anomaly" / "captcha": none)
- **10 `class="result__a"` anchors** → real result markup

Result anchors are `//duckduckgo.com/l/?uddg=<urlencoded>&rut=...` redirect wrappers:
```html
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fstripe.com%2F&amp;rut=357c...">Stripe | Financial Infrastructure to Grow Your Revenue
```
Top-3 organic results (uddg-resolved): `https://stripe.com/`, `https://stripe.com/payments`, `https://dashboard.stripe.com/` — **stripe.com is #1** (also #1 in the first run, where #2 was `stripe.com/en-ca`).

### (c) browser/patchright tier — **untested, reserved** (not used in this probe).

**Verdict: GO for the curl tier only.**

---

## What this means for T3a / T3b / T3c

- **T3a Wikidata + Wikipedia resolvers: GO.** Both shapes verified live with the honest UA. Build requirements the probe pinned:
  1. Entity pick must be **description-keyed** — label collisions are real (3/5 "Stripe"-labeled entities are a color band, a Gremlins character, a progamer).
  2. **Robots:** `/w/api.php` is robots-DISALLOWED on both hosts for generic UAs; ship scoped `http.robots_allow` entries (`{host: www.wikidata.org, path_prefix: /w/api.php}`, same for `en.wikipedia.org`) — mechanism exists in `config/default.yaml` — or `HttpFetcher`'s `respect_robots` will refuse every call.
  3. Wikipedia extlinks contain the apex but also blog deep links — apex-domain filter + first-extlink preference.
  4. P856 == wikipedia apex extlink (`https://stripe.com/` on both) is exactly the 2-source agreement the plan's auto-accept rule wants.
- **T3b GKG resolver: credential gate confirmed.** EKG keyless → 401 `UNAUTHENTICATED`/`CREDENTIALS_MISSING`; kgsearch keyless → 403 `PERMISSION_DENIED`. Ships as the credential-gated no-op until `GOOGLE_APPLICATION_CREDENTIALS` + `GKG_PROJECT_ID` exist; entity JSON-LD schema capture (website-url path) deferred to the first credentialed run — the EKG surface itself is live and the method name is confirmed via error metadata.
- **T3c DDG resolver: GO for the curl tier.** Body-validation contract to encode (case-insensitive): challenge = `"unfortunately, bots use duckduckgo"` / `"anomaly-detected"` / `"anomaly"` / `"captcha"`; success = anchors with `class="result__a"`; resolve `//duckduckgo.com/l/?uddg=` wrappers via the `uddg` query param before candidate ranking. Plain httpx tier: dead (202 + challenge body) — do not build. Browser tier: untested, reserved. stripe.com ranked #1 in both independent runs.

## Per-host request budget disclosure

Limit: ≤3 requests per host per rung (incl. robots). Observed (final run, 11 requests, 51.1 s):

| Host | rung 0 | rung 1 | rung 2 | rung 3 | rung 4 | rung 5 |
|------|--------|--------|--------|--------|--------|--------|
| www.wikidata.org | 1 | 2 | – | – | – | – |
| en.wikipedia.org | 1 | – | 2 | – | – | – |
| www.bing.com | 1 | – | – | – | – | – |
| enterpriseknowledgegraph.googleapis.com | – | – | – | 1 | – | – |
| kgsearch.googleapis.com | – | – | – | – | 1 | – |
| html.duckduckgo.com | – | – | – | – | – | 2 |

Max per host per rung: 2 (limit 3). Full session: **two** complete runs executed back-to-back (parser display fix between them) → each host received exactly 2× the table above, every pair ≥5 s apart. No flake retries were needed.

## Raw artifacts

- `data/probe/keyless_identity_robots_www_wikidata_org.txt` / `..._en_wikipedia_org.txt` / `..._www_bing_com.txt` — robots.txt dumps
- `data/probe/keyless_identity_wikidata_search.json` — wbsearchentities top-5 (collision evidence)
- `data/probe/keyless_identity_wikidata_claims.json` — Q7624104 P856 claim
- `data/probe/keyless_identity_wikipedia_search.json` / `..._extlinks.json` — search hits + 50 extlinks
- `data/probe/keyless_identity_ekg_error.json` — EKG 401 UNAUTHENTICATED body
- `data/probe/keyless_identity_kgsearch_error.json` — kgsearch 403 PERMISSION_DENIED body
- `data/probe/keyless_identity_ddg_plain.html` — plain-tier challenge body (202)
- `data/probe/keyless_identity_ddg_curl.html` — curl-tier result markup, first 200 KB (200)
- `data/probe/keyless_identity_findings.json` — machine-readable run summary (request log included)

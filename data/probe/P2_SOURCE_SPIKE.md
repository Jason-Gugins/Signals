# P2 Source Availability Spike — Findings (Task 6)

**Date:** 2026-08-31 · **Script:** `scripts/p2_source_spike.py` (paced, ≥5s between ANY two requests) · **Fetcher:** `src/core/curl_fetcher.py` (curl_cffi, `impersonate="chrome"`, Chrome 126 UA)

## Verdict table

| # | Source | Host(s) probed | Requests | Status | Rendering | Verdict | Gates |
|---|--------|----------------|----------|--------|-----------|---------|-------|
| 1 | Reddit | `www.reddit.com` + `old.reddit.com` | 2 (1+1) | 403 / 200 | challenge-blocked / login wall | **NO-GO → STUB fixture-only** | T7 |
| 2 | YC directory | `www.ycombinator.com` | 3 † | 200 / 200 / 404 | client-rendered (Inertia.js shell) | **STUB fixture-only** | T8 |
| 3 | Product Hunt | `www.producthunt.com` | 1 | 403 | challenge-blocked (Cloudflare) | **NO-GO → STUB fixture-only** | T10 |
| 4 | App-store reviews | `itunes.apple.com` + `play.google.com` | 5 total | 200 / 404 | server-rendered JSON / 404 | **GO plain-fetch (App Store RSS) / NO-GO (Play)** | T9 |
| 5 | BBB | `www.bbb.org` | 2 | 404 / 200 | server-rendered HTML | **GO plain-fetch** | T11 |

† Over-budget disclosure: 3 requests hit ycombinator.com (initial + 2 second-shape variants of the same URL). The plan's max is 2. All were ≥5s apart, sequential, and YC never served a challenge. No further YC requests will be made in T8 verification.

Pacing: every pair of consecutive requests in this whole run was ≥5s apart (script-enforced). No host returned an anti-bot challenge on a first request except Product Hunt (aborted immediately, per the abort rule) and www.reddit.com (403 network-policy block, aborted).

---

## 1. Reddit (gates T7 — `src/sources/community/reddit.py`)

**URLs probed (1+1, two hosts):**
- `https://www.reddit.com/r/sales.json?limit=25` → **403** (18.9 KB body)
- `https://old.reddit.com/r/sales/` → **200** but **login wall** (320.9 KB)

**Rendering:**
- www.reddit.com: **challenge-blocked (network policy, not CF).** Body is a shreddit-styled block page:
  ```html
  <div class="font-bold text-24 text-neutral-content-strong">You've been blocked by network security.</div>
  ```
  403 with zero JSON (`"title":`, `"t3_..."` selectors: 0 hits). Abort rule applied — not touched again.
- old.reddit.com: 200 but the "server-rendered" HTML is a **login interstitial** — `<title>Welcome to Reddit</title>`, 3 divs, 38 chars of visible text, `pageType=login` in the preload URL. Zero `thing`/`sitetable` post blocks. No post content is available unauthenticated.

**Extraction candidates:** none — no post markup or JSON was obtainable from either host. Do not guess selectors from memory (plan risk note).

**Verdict: NO-GO for live fetching → STUB fixture-only.** T7 builds a minimal synthetic fixture (old.reddit post-list markup written from known structure, flagged `# synthetic — live capture blocked`), parser on obvious semantic markup (`div.thing`, `a.title`), module docstring + README note the block. `community_reddit` stays `enabled: false`, cadence 24h.

---

## 2. YC batch directory (gates T8 — `src/sources/yc/collector.py`)

**URLs probed (3, † over budget as disclosed):**
- `https://www.ycombinator.com/companies?batch=S24` → **200**, 31.0 KB
- same URL with `X-Inertia: true` + `X-Inertia-Version` headers → **200**, JSON 1.4 KB
- `https://www.ycombinator.com/batch/S24` (legacy static batch page) → **404**

**Rendering: client-rendered — Inertia.js shell.** The page body contains only a mount-point div whose `data-page` JSON carries zero company data:
```html
<body class="ycdc2 companies index"><div data-page="{&quot;component&quot;:&quot;ycdc_new/pages/Companies/IndexPage&quot;,&quot;props&quot;:{&quot;env&quot;:&quot;production&quot;,&quot;currentBatch&quot;:&quot;Summer 2026&quot;},&quot;url&quot;:&quot;/companies?batch=S24&quot;,...}
```
The X-Inertia XHR returns the identical props (`env`, `currentBatch` only) — companies load through a further async call not exposed by either shape. No `/companies/` anchors, no `<table>` rows, no `__NEXT_DATA__` anywhere in the 31 KB. Legacy `/batch/S24`: `File Not Found | Y Combinator`.

**Extraction candidates:** none obtainable server-side.

**Verdict: STUB fixture-only.** T8 builds a synthetic Inertia `data-page` fixture, parser keyed on the obvious `data-page` JSON + semantic markup, with the honest limitation in the docstring + README (companies list not extractable without a browser tier).

---

## 3. Product Hunt (gates T10 — `src/sources/content/producthunt.py`)

**URL probed (1):** `https://www.producthunt.com/products/notion` → **403**, 6.0 KB

**Rendering: challenge-blocked — Cloudflare interstitial on the FIRST request:**
```html
<title>Just a moment...</title>
... script-src ... https://challenges.cloudflare.com; ... frame-src 'self' https://challenges.cloudflare.com
```

**Abort rule applied** — host untouched after this response.

**Extraction candidates:** none.

**Verdict: NO-GO → STUB fixture-only.** T10 gets a synthetic launch-page fixture (`data-test` attribute markup written from PH's known conventions), flagged synthetic, limitation in docstring + README.

---

## 4. App-store reviews (gates T9 — `src/sources/appstores/`)

**URLs probed (itunes.apple.com: 4 total — see request log; play.google.com: 1):**
- `https://itunes.apple.com/us/rss/customerreviews/id=409183871/sortby=mostrecent/json` → **200** but **0 entries** (app id 409183871 is dead: `lookup?id=409183871` → `resultCount: 0`)
- same feed `/atom` variant → **500** `<!-- Empty -->` (consistent with the dead id)
- `https://itunes.apple.com/us/rss/customerreviews/id=618783545/sortby=mostrecent/json?pagesize=5` (live app: Slack iOS) → **200**, 50 entries, 36.9 KB
- `https://play.google.com/store/apps/details?id=com.notion.app&hl=en&gl=US` → **404** ("Not Found" — com.notion.app is the wrong/currently-pulled package; the Play web reviews page is also known JS-rendered)

**Rendering: server-rendered JSON (GO) for the App Store RSS feed.** Full shape verified with the live-app fixture (`data/probe/p2_spike_itunes-reviews-slack.json`):
```json
"entry": [{ "title": {"label": "Loved the old slack, like the new"},
  "im:rating": {"label": "4"}, "im:version": {"label": "26.08.40"},
  "im:voteCount": {"label": "0"}, "author": {"name": {"label": "vshultz"}},
  "content": {"label": "Listen, the old Slack was the best..."},
  "id": {"label": "14490360492"}, "updated": {...} }]
```
Feed metadata for a dead id is an empty shell: `feed.author/update/rights/title/link/id` only, **no `entry` key** — that is the natural "no reviews" sentinel for the parser (vs an error).

**Extraction candidates (App Store RSS):** `feed.entry[].title.label`, `feed.entry[].im:rating.label`, `feed.entry[].author.name.label`, `feed.entry[].content.label`, `feed.entry[].id.label` (stable review id for dedupe).

**Verdict: GO plain-fetch for App Store RSS (public JSON, no anti-bot, works with curl_cffi); NO-GO for Play Store web reviews** (404 on the probed package; JS-rendered page per prior knowledge — do not build against it). T9: App Store path is real (`requires: app_store_id` account field, RSS feed per app, dedupe on review id); Play path is synthetic/stub with the honest limitation.

---

## 5. BBB profile pages (gates T11 — `src/sources/bbb/collector.py`)

**URLs probed (2):**
- guessed profile `.../us/ny/new-york/profile/software/notion-labs-inc-0121-87283855` → **404** (wrong URL shape — real pattern is `/us/<st>/<city>/profile/<category>/<name>-<area3>-<6-digit-id>`)
- real profile from search: `https://www.bbb.org/us/wa/seattle/profile/computer-software-developers/avalara-inc-1296-22018273` → **200**, 135.7 KB

**Rendering: server-rendered HTML, no challenge** (no `Just a moment`/DataDome/captcha markers in the body — the historical CF block does not apply to plain profile pages with curl_cffi chrome impersonation).

**Extraction candidates (actual snippets from the Avalara profile):**
```html
<dt>Local BBB:</dt><dd>BBB Great West + Pacific</dd>
<dt>BBB File Opened:</dt><dd>6/3/2005</dd>
<dt>Business Started:</dt><dd>8/11/1999</dd>
<dt>Type of Entity:</dt><dd>Corporation</dd>
<dt>Alternate Names:</dt><dd>Avalara AvaTax</dd>
```
```html
<div class="bpr-header-accreditation-rating" data-accredited="true">
<div class="bpr-header-rating" ...>
<script type="application/ld+json">  → {"@type": "LocalBusiness", "name": "Avalara Inc", ...}
```
JSON-LD LocalBusiness blob + `<dt>/<dd>` facts table + `bpr-header-*` rating/accreditation classes are the parse surface. Embedded state also carries `accredited_status":"AB","business_id":"22018273","business_name":"Aval...`.

**Verdict: GO plain-fetch.** T11 is a real collector: paced curl_cffi fetch of profile pages, parse `<dt>/<dd>` facts + accreditation flag + JSON-LD name; complaint lists may require deeper pagination (not probed — budget) — note as a T11 limitation.

---

## Raw artifacts

- `data/probe/p2_spike_reddit-json.json` — www.reddit 403 network-policy block page
- `data/probe/p2_spike_old-reddit-html.html` — old.reddit login wall (200, no content)
- `data/probe/p2_spike_yc-batch.html` — YC Inertia shell (31 KB, no companies)
- `data/probe/p2_spike_yc-inertia.json` — YC X-Inertia XHR (props: env/currentBatch only)
- `data/probe/p2_spike_yc-batchpage.html` — legacy /batch/S24 404 page
- `data/probe/p2_spike_ph-product.html` — PH Cloudflare challenge page
- `data/probe/p2_spike_itunes-rss.json` — reviews feed, dead app id (empty shell, no `entry`)
- `data/probe/p2_spike_itunes-atom.xml` — /atom variant 500 (dead id)
- `data/probe/p2_spike_itunes-reviews-slack.json` — live RSS fixture for T9 (Slack iOS, 50 reviews)
- `data/probe/p2_spike_play-reviews.html` — Play 404 page
- `data/probe/p2_spike_bbb-profile.html` — BBB 404 (guessed URL)
- `data/probe/p2_spike_bbb-avalara.html` — real BBB profile, 135 KB SSR
- `data/probe/p2_source_spike_findings.json` — machine-readable run summary

## Per-source build consequences (T7–T11)

- **T7 Reddit:** STUB fixture-only (`# synthetic — live capture blocked`), disabled-by-default config unchanged.
- **T8 YC:** STUB fixture-only on the Inertia `data-page` shape; collector can be upgraded to browser-tier later, out of scope now.
- **T9 App stores:** App Store RSS is a real GO (public JSON); Play Store is synthetic-only.
- **T10 Product Hunt:** STUB fixture-only (CF challenge, not probed further).
- **T11 BBB:** real GO — plain fetch + `<dt>/<dd>`/JSON-LD parse; complaints pagination unprobed.

# G2 Reviews Data Source — Probe Finding (Task 1 spike)

**Probe:** `scripts/probe_g2_reviews.py` — driven headed via Patchright (headless=False hardcoded)
against `https://www.g2.com/products/databricks/reviews`, with session cookies injected.

## Egress IP / reputation
- IP `99.226.66.20`, AS812 **Rogers Communications Canada Inc.** (Toronto, ON)
- Hostname `pool-99-226-66-20.cpe.net.cable.rogers.com` → **residential / ISP cable, NOT datacenter**
- Class: **residential (good for DataDome)**. No residential proxy was required.

## DataDome / challenge status
- DataDome is **cleared in headed mode** to the point that main pages returned HTTP 200 and a
  `datadome` cookie was present in the context.
- **BUT it is intermittent:** across probe runs the internal fragment XHR occasionally returned
  **403**, and in the final bounded run the main page was served a ~2.6 KB interstitial shell
  (title `g2.com`, no review content, no `reviews_and_filters` XHR fired at all). Residential IP
  helps but does not make a capture fully deterministic.

## DATA SOURCE: definitive finding
- **NOT** `__NEXT_DATA__` (no `__NEXT_DATA__` script, no `buildId` — `buildId = None`)
- **NOT** `self.__next_f` RSC flight payloads (none present, `rsc_chunk_count = 0`)
- **NOT** `<script type="application/json">` embedded JSON
- **NOT** a `/_next/data/<buildId>/...json` endpoint (none observed in the network log)
- **NOT** a bespoke JSON/GraphQL API
- Literal strings `reviewText` / `reviewLikes` / `starRating` / `reviewRate` were **absent** from
  every captured DOM.

### What WAS observed → **client-rendered DOM fed by an internal HTML fragment endpoint**
- The reviews page (an SPA) fetches the review cards as a **server-rendered HTML fragment** from:

  ```
  GET https://www.g2.com/products/databricks/reviews_and_filters        → 200 text/html
  ```
  Returns the rendered reviews fragment (review cards in `elv-*` component DOM), **not JSON**.
- The full reviews page HTML is:
  ```
  GET https://www.g2.com/products/databricks/reviews                    → 200 text/html
  ```

### Pagination / sort URL pattern (from network log)
- Sort: page navigates to `https://www.g2.com/products/databricks/reviews?sort=newest`
  which triggers fragment `.../reviews_and_filters?sort=newest` (sort param carried through).
- Page 2: `https://www.g2.com/products/databricks/reviews?page=2` →
  fragment `.../reviews_and_filters?page=2` (pagination param carried through).
- Pattern: query params `?sort=...` and `?page=N` are appended to BOTH the page URL and the
  `reviews_and_filters` fragment URL. There is NO Next.js `/_next/data/<buildId>` route.

### Review array / JSON path
- **Not captured.** No review-JSON array was extracted in any run (intermittent 403 / interstitial
  blocked the fragment body capture). The review content is HTML in the `reviews_and_filters`
  fragment, so a parser targeting this source should parse the rendered `elv-*` DOM (Task 1
  candidate C in the plan), not embedded JSON.
- One anomaly noted once: `final_url` briefly resolved to `/products/darktrace/reviews` (likely a
  "related products" client-side redirect); the databricks page was the requested target.

## Caveat
Because every run was DataDome-intermittent and the one clean 200 fragment response (first run)
was not body-saved, a single real review HTML/JSON object was not persisted. A future run when
DataDome fully clears should capture the `reviews_and_filters` 200 body to lock the exact DOM
matchers.

# Capterra Discovery Spike — Jira Reviews (Task 1)

- **Date:** 2026-08-30 (one browser session, ONE product, 1 attempt used of 3 max)
- **Target:** `https://www.capterra.com/p/19319/JIRA/reviews/`
- **Harness:** `scripts/capterra_probe.py` — headed Patchright, config UA, no cookie injection, homepage warm-up (~8s dwell + gentle scroll), then target, wait 8s, scroll ×3, capture `page.content()`.
- **Fixture:** `data/probe/capterra_jira_rendered.html` (772,368 bytes, 25 review cards). Sample card: `data/probe/capterra_sample_card.html`.

## (a) Protection stack: **Cloudflare only — no DataDome observed**

- HTTP **200** on every navigation (no challenge interstitial, no redirect off the target URL — `final_url` == requested URL).
- CF pass-through markers present but benign: `__cf_bm` cookie in the jar, `challenge-platform` script path. **No** `Just a moment`, no `cf-mitigated`, no `cf_chl_opt` → **no active CF challenge was served.**
- **Zero DataDome markers**: no `captcha-delivery.com`, no `var dd=`, no `datadome` cookie, no DataDome iframe. Third-party reports of DataDome on capterra.com appear **stale or wrong** as of this probe.
- Title rendered: `Jira Reviews 2026. Verified Reviews, Pros & Cons | Capterra` — real content page.

## (b) Did headed Patchright clear it? **Yes — first attempt, no retries**

One attempt. Homepage warm-up → direct nav to reviews page → real 772KB content page. The probe's automated "blocked" flag was a false positive (it keyed on the benign `__cf_bm`/`challenge-platform` pass-through markers); manual inspection confirms a clean clear. Attempts 2–3 were deliberately NOT run (GO SLOW: stop after a clear; don't re-probe a cooperating site).

## (c) Rendering model: **server-rendered (Next.js RSC) — reviews are IN the HTML**

- `self.__next_f` (Next.js App Router RSC streaming) present; no `__NEXT_DATA__`, no SPA shell (`id="root"`/`id="app"` absent).
- All 25 review cards, Pros/Cons paragraphs, star ratings, dates, and the total count (`Showing 1-25 of 15462 Reviews`) are present in the captured `page.content()`. No client-side fetch of reviews needed — the same DOM a plain `page.content()` sees contains everything a parser needs.
- Site is Next.js with `_next/image` optimizer and Emotion-style hashed classes (e.g. `e1xzmg0z c1ofrhif`) — **avoid styling hashes as selectors; they are build-specific.** Anchor on stable semantics.

## (d) Review-card selectors

- Container: `div[data-test-id="review-cards-container"]` (note: `data-test-id` hyphenated, NOT `data-testid`).
- Each card: direct-card wrapper `div.e1xzmg0z.c1ofrhif.typo-10.mb-6.space-y-4.p-6.lg:space-y-8` — hash class, fragile. **Stable parsing strategy:** iterate cards within `review-cards-container`, then extract by text semantics inside each card (see below). A robust short-term card selector: `div[data-test-id="review-cards-container"] > div > div` where the child contains `>Pros<`/`>Cons<` markers.
- In-card extraction cues (all present ×25):
  - Reviewer: `span.typo-20.text-neutral-99.font-semibold` (name), following text nodes = title/industry/tenure (`Used the software for: N+ years`); avatar `img[data-testid="reviewer-profile-pic"]` (this one IS `data-testid`).
  - Rating: `div[data-testid="Overall Rating-rating"]` containing `i[aria-label="star-full"|"star-empty"][data-rating="N"]` + numeric `span.e1xzmg0z.sr2r3oj` (e.g. `5.0`). Sub-ratings: `Ease of Use`, `Customer Service`, `Features`, `Value for Money` — same pattern.
  - Pros/Cons: `<span>Pros</span>` / `<span>Cons</span>` followed by sibling `<p>…</p>` (25 each on page 1).
  - Date: `<span class="typo-0 text-neutral-90 block">July 11, 2026</span>` immediately after the review body `<p>` — full month-name format, parse with a month-name parser (no `datetime` attr, no ISO in-card).
- 27 hash-class card occurrences vs 25 `>Pros<` — two extra matches are inside the RSC payload templates; parse the DOM, not raw string counts.

## (e) Pagination: **`?page=N` query param on the same URL**

Links found: `/p/19319/JIRA/reviews/?page=2` … `?page=100` (Jira total = 15,462 reviews, 25/page → 100 pages... actually 15462/25 ≈ 619, so the pager caps/segments — page 1 → next page 2; max linked page 100). `follow_tasks` should build `.../reviews/?page=N` incrementally from 2, stop at empty page or `max_review_pages`.

## (f) Consent gate: **none observed**

OneTrust SDK scripts are referenced (603 `onetrust` string hits — all in script payloads), but no visible banner/dialog was detected (probe checked `#onetrust-banner-sdk`, `#onetrust-consent-sdk`, Didomi, Cookiebot, and generic `[id/class*=consent]` selectors — all empty/hidden). Likely geo-gated (EU-only) or lazy. **Probe took no action; a consent-click handler is probably unnecessary from a US IP but the fetcher should tolerate the banner if it appears** (click `#onetrust-accept-btn-handler` if `#onetrust-banner-sdk` is ever visible).

## (g) Full-page size

772,368 bytes rendered HTML; 25 review cards on page 1; response HTTP 200 throughout.

## (h) Rate-limit / escalation signals

None observed in this single session. One navigation burst only; no 403/429, no challenge, no CAPTCHA. Third-party ~4s/request floor is unverified here — keep paced cool-downs anyway (CF Bot Management IS live, just not triggered at this pace from this residential IP).

## Bottom line for Tasks 2–6

Capterra is **easier than G2**: CF-only, headed Patchright clears on attempt 1, reviews are server-rendered in one 772KB HTML per page, pagination is plain `?page=N`. Parser should anchor on `div[data-test-id="review-cards-container"]` + in-card text semantics (`Pros`/`Cons`/`star-full`/date span), never on Emotion hash classes or the `data-testid` vs `data-test-id` mix-up (both spellings exist).

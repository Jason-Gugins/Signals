# Capterra Search Probe — Findings (Task 1 spike)

**Date:** 2026-08-30 · **Script:** `scripts/capterra_search_probe.py` (max 3 paced requests, 5s apart) · **Fetcher:** `src/core/curl_fetcher.py` (curl_cffi, `impersonate="chrome"`)

## Working endpoint

```
GET https://www.capterra.com/search/?query=<url-encoded company name>
```

- **Status 200** with plain `curl_cffi` chrome impersonation + standard Chrome UA. No CF/DataDome challenge encountered.
- Results are **server-rendered HTML** (a Next.js App Router page — no `__NEXT_DATA__`, but an embedded RSC/flight payload with escaped product-card JSON). Not JS-only.
- `https://www.capterra.com/api/search?q=...` → **404**; there is no obvious public search JSON API, and none is needed.

## Extraction method

Regex over the response body for `/p/(\d+)/([A-Za-z0-9-]+)` segments. Two useful tiers:

1. **Anchors** — `href="/p/<id>/<Slug>/"` real `<a>` tags render in the initial HTML (13 in the jira test). Best selector for Task 5: `a[data-testid="thumbnail-link"]` inside cards marked `data-testid="search-product-card"`; the RSC payload also embeds `https://www.capterra.com/p/19319/JIRA/` as an escaped `href` field.
2. **Whole-payload regex** — also matches `/p/<id>/<Slug>` inside the embedded RSC JSON (23 segments for "jira"), picking up products beyond the visible cards.

Verified example: query `jira` → top card is `/p/19319/JIRA/` (canonical form confirmed: numeric id + title-case slug, trailing slash; reviews at `/p/<id>/<Slug>/reviews/`).

## Ambiguity strategy for multiple matches

Same pattern as `src/identity/g2_resolve.py::resolve_g2_slug`:

1. Exact case-insensitive match of product **name** against the company name.
2. Substring / normalized match (strip spaces, hyphens; compare against slug with `-`→space).
3. First result in document order (search ranking) — Capterra's own relevance order.
4. When scores tie or several products plausibly match (e.g. "JIRA" vs "JIRA Service Management" `227102/JIRA-Service-Management`), **do not guess**: return the candidate list and mark the account unresolved (needs human / LLM disambiguation), mirroring G2's ambiguity handling. Note the family-pollution risk: queries return marketplace add-ons (e.g. `10039800/Jira-Backup-and-Restore`), so restrict candidates to cards from `search-product-card` testids, not the whole-payload regex.

## Patchright fallback needed?

**No.** Server-rendered HTML fetches cleanly with curl_cffi chrome impersonation (consistent with Aug 2026 reviews-page behavior). Reserve Patchright only for future anti-bot escalation; the `curl_fetcher.py` wrapper covers this endpoint.

## Raw artifacts

- `data/probe/capterra_search_jira.html` — search page for "jira" (436 KB, SSR + RSC payload)
- `data/probe/capterra_search_findings.json` — probe run summary

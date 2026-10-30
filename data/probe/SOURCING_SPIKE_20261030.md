# Sourcing spike (2026-10-30) — arm verdicts

Probes: tmp/sourcing_spike.py (free HTTP, robots checked per arm).

## Arm A — Greenhouse boards-api content: **GO (emphatic)**

- `boards-api.greenhouse.io/v1/boards/databricks/jobs?content=false` → 200,
  903 jobs (board token `databricks` resolves first try).
- `?content=true` → 200, **9,920,049 bytes, 903/903 jobs carry content**,
  **7,313,406 chars of stripped prose** across the board.
- Per-job endpoint `/jobs/{gh_jid}` → 200 (gh_jid 8805655002 from the
  stored envelope verified: 5,605 chars of stripped content).
- Content is HTML-escaped HTML (`&lt;p&gt;…`) — extraction must unescape
  (parse_greenhouse already does `strip_html(unescape(content))`).
- Robots: databricks.com/robots.txt does NOT block /careers;
  boards-api robots.txt 200 (api host crawlable).

**Re-scope:** the collector (src/sources/ats/collector.py:44) already
requests `content=true` and parse_greenhouse already handles content —
the build task is to reconcile why the corpus envelope (source
`ats_discovery`, doc d8419437…) lacks content (the discovery path fetched
the careers-page embedded JSON, not the boards-api list), then extend
doccontent's json_jobs extraction to carry unescaped, stripped job
content (capped) into the implement seam.

## Arm B — blog RSS: **NO-GO**

rillet.com/blog/rss.xml, /rss.xml, /feed, /blog/feed → all 404 (the site
serves a 98KB SPA 404 page). robots does not disallow /blog but no feed
exists. Blog bodies fold into arm C's URL-pattern fetcher. The
company_feed zero-code path is unavailable for rillet.

## Arm C — content pages: **GO**

- rillet.com/case-studies/vsee-… → 200, 178KB, server-rendered (no
  __NEXT_DATA__ shell, no app-root), 84KB stripped.
- rillet.com/compare/netsuite → 200, 194KB, 93KB stripped.
- Stripped samples contain real prose alongside script residue —
  html_to_text-based extraction with the standard fetcher applies.
- rillet robots does not disallow /blog or /case-studies.

**Re-scope:** new bounded `sitecontent` source adapter (sitemap-enumerated
URLs × pattern filter, per-cycle cap), RSS sub-path dropped (404s).

## Build scope after verdicts

- Task 2 (Arm A): reconcile the greenhouse collection path so the corpus
  envelope is the boards-api content=true response; doccontent json_jobs
  gains an unescape+strip `content` branch (capped per job).
- Task 3 (Arm C): `sitecontent` adapter (case-studies/blog/compare/
  solutions patterns, per-cycle cap 20, cadence-gated); arm B folded in
  (/blog/ pattern, no RSS).
- Task 4: growth pass + re-harvest — G5 nouls on need-bearing prose.

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

## Validation harvest (2026-10-30, post wave — the buried question answered)

Packages: rillet.com-20261030T222338239549Z, databricks.com-20261030T222506470852Z
(growth pass first: databricks 228 docs, rillet 116 docs, coverage 1.00;
greenhouse content-bearing envelope verified in-store — 902/902 jobs with
content, extraction yields 120KB of real prose).

**Content reached the gates (proven by the dual-hash join):** rillet staged
19 docs — 11 sitecontent pages, plus ashby/wayback/news/itunes envelopes;
databricks staged 15 docs — **7 ats_greenhouse content-bearing envelopes**.

**But the claim pool is still not promotable, for two NEW reasons now
precisely located:**

1. **rillet's accepted claims (8) are ALL wayback crawl-log statements**
   ("the earliest record of rillet.com in this log is a crawl on
   2018-03-16… HTTP 200") — a NEW metadata class winning retrieval because
   crawl logs are dense with identity terms. The sitecontent pages staged
   but produced no accepted claims (held by the output screen or judged
   says-nothing — their content is customer-proof/competitive intel, which
   the taxonomy has no promotable type for).
2. **databricks: 0 accepted claims** despite 7 content-bearing greenhouse
   envelopes staged — the batch output screen held the rich-prose batches
   (20 reviewed). The output screen is now the binding reviewer on the
   BEST material in the corpus.

**G5 was barely exercised** (8 calls, nouls 0.08–0.13, dropped) — the
accepted-claim pool feeding it is still weak, so the band question remains
open until the screen/calibration lets rich prose through.

## Wave verdict

Sourcing SHIPPED and VALIDATED: the fetch machinery works (content-bearing
envelopes + 40 sitecontent pages in-store, staged, joined via dual-hash).
The constraint chain is now fully mapped and each link measured:
corpus material (fixed) → staging composition (retrieval pulls wayback
logs — a query/selection question) → output screen (holds rich-prose
batches — the calibration lever) → G5 band (untested on rich prose until
the screen lets it through). The decide-lab's opening targets are now
exactly: output-screen thresholds, retrieval query composition (or a
wayback-log exclusion), then the G5 band.

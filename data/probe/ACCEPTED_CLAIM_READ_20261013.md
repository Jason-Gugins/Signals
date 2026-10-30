# The accepted-claims read (2026-10-13) — the fork decision

Question under test: of the post-fix G4-accepted claims (the claims Jev
scored 0.08–0.29 at G5 and dropped), do ANY warrant promotion — i.e., is
the G5 band miscalibrated, or is the material genuinely weak?

Dataset: all 79 unique accepted claims across the three post-fix runs
(databricks 20261012T1905, rillet 20261012T1909, rillet 20261029T2135).
Judged as the salesperson for the AB-test seller (AI infra + GTM tooling
into AI-native B2B): would this true, verified claim justify a sales play
— does it reveal a need, pain, or buying window?

## Verdict: 0 of 79 claims warrant promotion. G5 was right.

Breakdown of all 79:

| class | count | examples |
|---|---|---|
| Envelope catalog restatements (sitemap URL slugs verbalized: "Rillet offers accounts payable functionality as a product" — quote is the URL) | ~38 | `rillet.com/product/accounts-payable` → "Rillet offers accounts payable" |
| Corporate-structure metadata (subsidiary entities from ATS JSON: Japan K.K., India Pvt, Korea LLC, Australia, UK, Federal, GmbH) | 8 | "Databricks operates in Germany through Databricks GmbH" |
| Robots/crawl-policy statements (AI-crawler allowlists, llms.txt, Content-Signal, utm hygiene) | 10 | "Rillet permits ChatGPT-User… to crawl its site" |
| Wrong-entity (Zibma Infotech's "Rillet Education" app from an iTunes result) | 5 | "Students and parents can use the app to view daily attendance…" |
| Job-category metadata (Field Engineering category, FDE function, BCG alliance RVP, university program) | 9 | "Databricks classifies a large share of its openings under Field Engineering" |
| Product-investment restatements (job titles verbalized: Lakebase PM, Genie SA, security SA, vertical DSAs, Lakewatch PMM) | 9 | "Databricks fields specialist presales architects focused on security" |

Best-of-set (the only three with any salesperson-relevant content):
1. "Rillet raised a $100M Series C at a $1B valuation" — a real funding
   event and growth marker. But: restated from a SITEMAP URL slug, and
   deterministic sources (Form D, news classification) already own funding
   events. Duplicative, and ungrounded (URL as citation).
2. "VSee cut its month-end close from 30 days to 4 on Rillet" — a real
   outcome story, and the closest thing to pain language in the set — but
   the pain is VSee's (Rillet's customer), not a need of the prospect.
3. "AI Forward Deployed Engineer - London" — mild AI-expansion signal.

None of the 79 expresses a need, pain, budget event, or buying window for
the account. Jev scoring them 0.08–0.29 was, claim by claim, honest work.

## Why the material is this weak — the structural cause

The accepted-claim provenance is four MACHINE FORMATS:

| source format | docs | what the implementer actually had |
|---|---|---|
| ATS API JSON (Greenhouse) | 1 doc → 21 claims | an envelope whose bulk is `absolute_url`/`metadata` scaffolding; the human-readable job DESCRIPTIONS are buried in content fields |
| sitemap XML | 2 docs → ~38 claims | a URL LIST — `<loc>` paths verbalized as sentences ("Rillet offers X" from `/product/x`) |
| iTunes API JSON | 1 doc → 5 claims | an app-store result for a DIFFERENT "Rillet" app |
| robots.txt / RSS | rest | policy text and channel headers |

The G4 document gate passed these honestly: they ARE about the account,
they DO contain verbatim-quotable spans, and post-Branch-E Jev sees real
context. But there is no need content in a sitemap. ~80% of the accepted
claims are restatements of machine metadata because machine metadata is
most of what the implementer was given. Branch I's prompt prohibition
killed the pure "resultCount of 4" claims but cannot stop URL-slug
verbalization when the URL list is the document.

## The fork decision

**CORPUS/EXTRACTION, not calibration.** Concretely, the next wave is
"content extraction before claims":

1. **Envelope-aware text extraction**: before the implementer sees a doc,
   extract its HUMAN CONTENT by format — ATS JSON → the `title` +
   `description` fields per job; RSS → item titles/descriptions; iTunes →
   app description field; sitemap XML → flag as catalog (no extraction —
   or title-slugs only, clearly labeled); robots.txt → policy summary at
   most. The doc gate and implementer then judge human prose, and the
   claims stop being envelope restatements.
2. **Wrong-entity guard at the document level**: the iTunes "Rillet
   Education" doc should never reach G4 for rillet.com — a
   developer/publisher check (the envelope names a different vendor) or an
   entity-alignment screen at doc-gate time. Wrong-entity claims are worse
   than no claims.
3. **G5 band stays 0.30.** Do not touch it. The band is not the problem;
   it correctly rejected this material. Revisit only after extraction
   ships and real need-bearing prose reaches G5.

What this read does NOT decide: whether real need-bearing prose (actual
job descriptions, blog bodies, case-study pages) exists in these sources —
it almost certainly does (the Greenhouse envelope contains full job
descriptions; the case-study pages have bodies). The extraction wave will
answer how much promotable material was buried.

## Note on judgment provenance

This read was performed by the AI orchestrator as salesperson stand-in for
the AB-test seller (a fictional profile). The structural conclusion does
not depend on taste: the provenance table (machine formats → metadata
restatements) is mechanical, and the wrong-entity class is objective. The
user is the final calibration on the 2–3 borderline items.

## Extraction validation (2026-10-30, post extraction-before-claims wave)

Harvest: rillet.com-20261030T000643278490Z + databricks.com-20261030T000751095097Z
(frozen corpus, enforce, retrieval staging, persist on).

- **Deterministic assertions — ALL PASS.** rillet's stage record: "1 catalog
  docs and 1 wrong-entity docs / 1 wrong-entity items excluded before the
  gate" — the sitemap (376-URL catalog) and the Zibma iTunes result were
  excluded before any Jev spend, exactly as the read predicted. Accepted
  claims containing Zibma/education content: **0** (was 5). Slug/marketing
  restatements: **0** (was ~38).
- **The buried question, answered**: the envelopes contained almost no
  promotable prose. Rillet's post-extraction accepted claims are all
  robots.txt policy statements (AI-crawler allow/block, llms.txt) — the
  only human prose its envelopes hold after exclusions. The genuinely
  need-bearing content (job DESCRIPTIONS, case-study page bodies, blog
  posts) was never fetched as documents: the sitemap merely listed those
  pages. **The promotion path now requires detail-page fetching** — a
  source-level follow-up (staged in the extraction plan).
- **databricks**: 26 G4 rows, all reviewed by the output screen, 0
  accepted — the output screen remains the binding reviewer at this
  domain. degraded=False, zero errored rows (the typing fix holds).
- **G5**: 5 calls (rillet), nouls 0.09–0.14, all dropped — even the
  cleanest available claims don't approach the 0.30 band. Consistent with
  the read: the band is not miscalibrated; the material was the problem.

## Final wave verdict

The extraction wave worked exactly as designed: catalog and wrong-entity
material is structurally excluded, and the first honest post-extraction
claim pool is small, clean, and weak — which settles the fork: the gates
were never miscalibrated; the corpus lacked promotable content. The
decision record now supports the next investment: detail-page fetching
(and, when real need-bearing prose exists, the decide-lab's band/criteria
calibration on material that can actually clear it).

# Signals

Signals is a sales signal enrichment engine: it continuously collects buying
signals about your target accounts from ~30 self-built, **zero-cost** sources.
It resolves one identity per account, scores and tiers the signals per High
Probability Prospecting (HPP), and maps each signal to a sales play. The
output is a cited dossier per account under `data/dossiers/`, plus ranked
account briefs you can act on. Signals exists because commercial signal
platforms are priced per seat and still need gluing together.

## Why zero-cost?

No paid APIs: no ZoomInfo, Apollo, Exa, BuiltWith, or Bombora. The only keyed
integrations are free, and both stay off until credentials exist. `GITHUB_TOKEN`
raises the GitHub API rate limit. Google's Knowledge Graph APIs serve identity
discovery (see *Identity discovery* below). Two documented exceptions carry
paid credentials: the optional captcha-solver keys in *Cloudflare bypass* and
the metered LLM decision layer (see its *Cost* paragraph). Both are also off
until configured.

## How it stays polite

- Public business data only.
- `robots.txt` honored by default.
- A real contact address goes in the User-Agent (`SIGNALS_CONTACT_EMAIL`).
- Rate limits are floors, not targets.
- **The first documented robots exception: the DuckDuckGo SERP stage of
  `sweep --discover --ddg`.** duckduckgo.com's robots.txt disallows scraping
  and the curl tier carries no robots logic, so the stage ships off by
  default. It runs only at resolve time, one request per lookup. It never
  runs on a collection cadence, and it is probe-gated per
  `data/probe/KEYLESS_IDENTITY_2026_09.md`.
- **The second documented robots exception: the opt-in `xray` command.**
  Manual, self-paced, never scheduled. It fetches DuckDuckGo's LITE SERP; see
  *X-ray SERP prospecting* below.

Marketplace scraping is opt-in — see the *Sources* section below for the full
gate and per-site options.

## Quick start

Everything a first session needs, in order. The pipeline has six stages:

`seed` → `resolve` → `collect` → `score` → `brief` → `export`

- `seed`: load the accounts you care about.
- `resolve`: fill each account's identity fields — CIK (SEC filer ID), ATS
  vendor/token, feeds, ICP (ideal customer profile).
- `collect`: run every enabled source the account qualifies for.
- `score`: classify each signal and tier the account.
- `brief`: turn the top signals into sales plays — one cited brief per account.
- `export`: write the results out to `data/exports/`.

Work through them in order.

### 1. Clone and install

Windows 10. Python 3.12.

```powershell
git clone https://github.com/Jason-Gugins/Signals.git
cd Signals
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -e ".[rerank]"   # optional: cross-encoder rerank extra
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m patchright install chromium
copy .env.example .env
# set SIGNALS_CONTACT_EMAIL to a real address (SEC EDGAR 403s without it)
# set GITHUB_TOKEN to raise community_github from 60 to 5,000 req/hr (optional)
```

### 2. Verify

```powershell
.\.venv\Scripts\python.exe -m src.cli doctor --no-network
.\.venv\Scripts\python.exe -m pytest -q
```

Healthy looks like: no FAIL lines from the doctor, and no failures from the
test suite. A few WARN lines on a fresh install are expected — they flag
optional extras you haven't configured.

`pip install -e .` also installs a `signals` console script — a shorthand for
the `python -m src.cli` invocations shown throughout this README.

CI runs the same suite on every push/PR (Ubuntu + Windows matrix, offline
lane) plus a nightly live-parity lane — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### 3. Init and file drops

```powershell
.\.venv\Scripts\python.exe -m src.cli init
```

Three of the files below ship as tracked, comment-only stubs: the `.txt`
lists. The x-ray string library `config/lists/xray_strings.yaml` is tracked
too — see *X-ray SERP prospecting* below. Edit the stubs in place rather than
create them. `champions.csv` and anything else you drop in stay untracked — a
human curates. See [config/lists/README.md](config/lists/README.md) for the
authoritative list:

- `config/lists/competitors.txt` — domains that should never be scored as prospects
- `config/lists/customers.txt` — existing customers (route to CS)
- `config/lists/dnc.txt` — do-not-contact
- `config/lists/champions.csv` — prior buyers; unlocks `champion_migration`
- `data/inbox/owned/*.csv|*.jsonl` — first-party intent (web/ESP export)

Once you've dropped your own `champions.csv`, load it with
`.\.venv\Scripts\python.exe -m src.cli champions --load config/lists/champions.csv`.

Next: load accounts — see the `seed` example under *Everyday commands*.

## Everyday commands

**Enrichment flows & waterfalls (per-domain playbooks): see [ENRICHMENT.md](ENRICHMENT.md).**

`--dry-run` goes **before** the subcommand (`python -m src.cli --dry-run collect`).

The core loop:

```powershell
.\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv --linkedin --repvue
#   --linkedin: also read the companion scraper's local DB (silent no-op
#   without the ../Linkedin checkout); --repvue: seed from a local repvue DB
.\.venv\Scripts\python.exe -m src.cli run --cohort canada-software
.\.venv\Scripts\python.exe -m src.cli brief --domain acme.com
.\.venv\Scripts\python.exe -m src.cli export --format csv
.\.venv\Scripts\python.exe -m src.cli watch --once
```

`sweep` is one-command onboarding: it seeds or updates the account from a URL or
bare domain, then runs every enabled source for it. `--force` recollects even if
the account already exists. `--deep` extends the sweep to existing accounts —
resolve pass and collect in one command. Bare company names are refused; use
`sweep --discover "Name"` or `sweep --competitors "Name"` for name-keyed
discovery (see *Identity discovery* below). If an account is missing fields a
source requires, the run reports that source as skipped. LinkedIn deepen stays
a separate manual `deepen` call.

One row per command:

| Command | What it does |
| --- | --- |
| `seed` | Load the accounts you care about into the registry. |
| `run` | Run the pipeline for a cohort: resolve, collect, score, brief, export. |
| `brief` | Turn an account's top signals into sales plays — one cited brief. |
| `export` | Write results to `data/exports/` (`--format csv` or `json`; `--what` picks a subset). |
| `watch` | Loop the pipeline on an interval (`--interval-minutes`, default 60); `--once` runs a single pass. |
| `init` | Create the `data/` directory tree and the SQLite database. |
| `collect` | Run every enabled source the account qualifies for. |
| `resolve` | Fill identity fields: CIK, ATS vendor/token, feeds, ICP — `--cik`/`--ats`/`--feeds`/`--icp` are always-on. `--g2`/`--capterra` discover marketplace slugs via each site's search; opt-ins below. |
| `find-careers` | Careers-page discovery for one domain (`find-careers DOMAIN`) — sitemap-first; prints the URL and which rung found it. |
| `reparse` | Re-run parsers over already-stored raw documents (`--source`, `--since`). |
| `index` | Build the hybrid-retrieval index (chunks + FTS5 + local embeddings). It is the prerequisite for the default-on retrieval staging — explicit, budgeted, resumable, never lazy inside `intel`; see *Hybrid retrieval staging*. |
| `score` | Classify each signal and tier the account. |
| `status` | Health report: storage quota and per-source fetch success rates. |
| `doctor` | Environment health check; `--no-network` skips the network checks. |
| `accounts` | List registered accounts: domain, name, tier, score. |
| `champions` | Load prior buyers from CSV (`--load`). |
| `signals` | Print an account's stored signals (`--domain` required; `--type`, `--since` filter). |
| `deepen` | Trigger the companion LinkedIn scraper for one company (manual, gated). |
| `g2-export` | Export G2 reviews to JSON and/or CSV. |
| `g2-selfcheck` | Fetch one G2 product live and verify the parser still works. |
| `capterra-selfcheck` | The same drift check for Capterra — plain HTTP fetch, no stealth browser. |
| `ats-set` | Manually stamp `ats_vendor`/`ats_token` on an existing account (`ats-set DOMAIN VENDOR TOKEN`) — never creates one. Workday compound tokens contain `/`. |
| `prune` | Retention: finalize `runs` rows stuck in `running` past `--stale-run-hours` (default 6); delete old fetch_log/documents/runs rows and raw files past `--keep-days`; WAL checkpoint + ANALYZE. `--vacuum` reclaims space. |
| `plays` | Record a local play outcome for backtesting: `--outcome hit` or `miss`, plus `--domain X --play Y`. |
| `plays-report` | Print per-play sent/hit/rate numbers from recorded plays (read-only). |
| `plays-calibrate` | Feed decided play outcomes into the calibration table (see *Signals and the backtesting loop*). |
| `digest` | Per-account markdown alert digest in `data/digests/` (`--period daily` or `weekly`, `--domain X`). `--tier-4` includes dormant tier-4 accounts as nurture digests with no plays; `--email` sends it via SMTP. |
| `selfcheck` | Generic five-state selector-drift check (`--source techstack`, `news_rss`, or `ats_greenhouse`). Challenge/empty exit 0; drift/error exit 1. |

The `resolve` opt-ins, spelled out: `--appstore` via iTunes Search, `--bbb` via
the BBB search API, and `--linkedin` — slug discovery through the companion
scraper's `discover` human-triggered posture, no automated login.

`index` takes `--domain X`: `--limit N` budgets a partial backfill, `--stats`
reports per-domain coverage without indexing, and `--rebuild-fts` regenerates
the FTS shadow.

Where things land:

- Exports, briefs, digests, and alerts go to `data/exports/`, `data/briefs/`,
  `data/digests/`, and `data/alerts/`.
- Run logs rotate in `data/logs/`, keyed by run ID — 10 MB rotation, configured
  via `logging.logs_dir`.
- Raw bytes live in `data/raw/<xx>/<sha>.gz` — content-addressed gzip.

## Master intelligence flow (`intel`)

`intel` is the master intelligence flow: one account, every capability, one
dossier.

```powershell
.\.venv\Scripts\python.exe -m src.cli intel acme.com
.\.venv\Scripts\python.exe -m src.cli intel acme.com --name "Acme Inc" --force
.\.venv\Scripts\python.exe -m src.cli --dry-run intel acme.com
```

Five stages, always in this order:

1. `identity` — the CIK/ATS/feeds/ICP resolver pass for the domain
2. `collect` — every enabled source the account qualifies for (cadence-aware)
3. `derive` — the local derived pass (`jobsignals` + `needs`) against the seller-market profile
4. `score` — classify, score and tier the account
5. `package` — build and write the dossier

**Flags.**

| Flag | Meaning |
| --- | --- |
| `--name NAME` | Company name for the name-matching sources. |
| `--market-profile ID` | A profile id from `config/markets.yaml`. |
| `--skip STAGE` | Repeatable. Skips any of `identity`, `collect`, `derive`, `score`, `package`. |
| `--force` | Ignores source cadences. |
| `--max-signals N` | Caps the active signals in the package. |
| `--no-write` | Builds the dossier but writes nothing. |
| `--with-linkedin-resolve` | Off by default — see **Opt-in posture** below. |
| `--with-marketplaces` | Off by default — see **Opt-in posture** below. |
| `--include-fanout` | Includes the global fanout sources `sec_formd`, `federal_register`, `warn_notices`. Those plan the whole filing universe rather than one account, so they stay off for `intel` without this flag. |
| `--with-llm` | The opt-in LLM decision layer — see the next section. |

A skipped run records a gap naming those sources. An included run reports how many accounts the
fanout seeded.

**Output.** The package is a portable directory under the configured dossiers dir
(`data/dossiers/` by default). It holds `manifest.json`, `dossier.json`, `dossier.md`,
`evidence.jsonl` and `prompt.md`. Every claim in it cites an evidence record.

**Dry run.** `--dry-run` goes before the subcommand, like every command. It requires an account
that already exists — an unknown domain is refused with `intel refused: ...` and a non-zero exit.
It performs planning only: it plans the collection and writes nothing.

**Opt-in posture.** Two capabilities stay off unless you ask for them: LinkedIn slug discovery
(`--with-linkedin-resolve`) and the marketplace adapters (`--with-marketplaces`). LinkedIn
resolution is human-triggered by design — no automated login. The marketplace set is pace-limited
and anti-bot aware. When either is skipped, the run records a coverage gap rather than leaving a
silent hole.

The single ATS/careers discovery ladder is capped at 10 requests.

**Seller-market profiles.** `needs` / `required_stack_demand` come from the seller-market profile
selected by `--market-profile`, read from `config/markets.yaml`. That file ships empty on
purpose. Until it is filled in, those sources promote nothing and `intel` reports a
"no relevance vocabulary configured" gap.

### Hybrid retrieval staging (on by default)

Left alone, the implement stage (the document gate + implementers, next section) screens the 200
**newest** stored documents for the account — a pure recency cut. A six-month-old but highly
relevant page (a funding announcement, a needs statement) falls off that tail, while newer
irrelevant pages each cost a document-gate request. The `retrieval:` block stages the most
**relevant** documents instead. It has been **on by default** since the 2026-10 A/B test:
identical G4/G5 outcomes at 75–79% lower Jev gate load
(`data/probe/RETRIEVAL_AB_2026_10.md`).

SQLite FTS5 BM25 wins on exact tokens — vendor names, filing codes like `8-K`, proper nouns.
Local MiniLM cosine embeddings win on paraphrases — "hiring head of sales" ↔ "VP Sales role".
Reciprocal Rank Fusion merges the two lists, and the ms-marco cross-encoder reranks the
shortlist. MMR suppresses the near-duplicates content-addressed storage mints — it stores a new
document per byte-diff, so identical boilerplate can exist N times.

The top `stage_docs: 35` survivors flow through the same document gate, G3–G5 and citation chain
as before. Retrieval reselects the candidates; it never adds a spend path. 35 is not a round
number: it is the `decider.max_decide_tokens_per_run` break-even with every wave-2 battery
enabled (~10.3k tokens/doc against the 400k run ceiling). Raise it only when batteries are
disabled in `config/decide.yaml`.

**Prerequisites.** Retrieval needs the `[rerank]` extra and a built index.
`pip install -e ".[rerank]"` — the extra carries onnxruntime/tokenizers. Then
`python -m src.cli index --domain acme.com`; the first run downloads the ~25MB
embedding model once, HF-cached like the reranker. Without the extra the vector
leg is disabled and `doctor` warns via `retrieval_readiness`. The query reuses
the account identity plus the seller-market vocabulary — the profile you
already wrote for `needs` steers retrieval for free. `candidate_limit: 180`
(chunks per leg before fusion) is the real staging selector — 60 under-selected
at 5–19 docs in the A/B.

**Fresh installs.** With retrieval on and no index built yet, `--with-llm` runs
stage the recency tail and record an honest gap naming the coverage and the
remediation (`python -m src.cli index --domain <domain>`). `doctor` shows the
matching `retrieval_readiness` WARN. That WARN is the intended onboarding
signal, not an error — build the index and it goes green (`retrieval enabled;
index ready (N chunks)`).

**Honest fallback.** Retrieval problems can never fail an `intel` run. Below
`min_index_coverage` (0.8), with an unavailable leg, or on any retrieval error,
the stage stages the 200 newest documents exactly as before. The stage record's
`retrieval.staged_by` always says which selection ran: `retrieval`, `uncovered`,
`unavailable`, or `error`.

**Turning it off.** Set `retrieval.enabled: false` in `config/default.yaml`; a yaml file with no
retrieval block is also off. The stage record then carries no `retrieval` key at all — the run is
byte-identical to the legacy 200-newest staging.

**Cost.** 35 relevant documents replace the 200-doc recency tail, so document-gate and
implementer spend goes down, not up. The token pre-flight bills the smaller staged list
automatically. Indexing is the only extra step, and it is explicit.

## LLM decision layer (opt-in)

`intel` carries an optional agentic layer. A MiMo planner narrows and orders source scope. DeepSeek/GLM implementers atomize already-stored documents into cited claims. A TypeSafe Jev typed-decision gate sits at the stage boundaries.

The layer exists only inside `intel` runs — `watch` and scheduled `collect` cycles never invoke it. It is OFF by default and inert unless BOTH conditions hold: `--with-llm` is passed, and `config/decide.yaml` sets `mode:` to `shadow` or `enforce`. The env var `SIGNALS_DECIDE_MODE` overrides the config.

At `mode: "off"` (or without `--with-llm`), `intel` behaves exactly as it does without this section. One extra config load is the only difference. If the layer is requested but keys or consent are missing, the deterministic pipeline is unchanged and no outbound call is made. The run records gaps plus decide provenance.

Opt in when you want planner-narrowed collection and LLM-atomized cited claims on top of the dossier. Stay at `mode: "off"` when you need byte-identical, reproducible runs or zero outbound calls.

```yaml
# config/decide.yaml — ships mode: "off"
mode: "off"               # off | shadow | enforce
planner:
  model: xiaomi/mimo-v2.6-pro                 # OpenRouter
implementers:
  bulk:      { model: deepseek/deepseek-v4.1-flash }              # claim atomization
  reasoning: { model: z-ai/glm-5.3-flash, reasoning_effort: max } # five dossier fields
decider:
  model: jev-1.13.0       # TypeSafe Jev (api.typesafe.ai), bills input tokens only
```

**Keys and consent.** Keys live in an untracked `.env`, never committed. You need `OPENROUTER_API_KEY` (planner + implementers) and `TYPESAFE_API_KEY` (Jev). For ANY live outbound LLM call, shadow mode included, `SIGNALS_DECIDE_DATA_EXIT` must equal the exact string `I-understand-document-text-leaves-this-machine`. All three are pre-listed in `.env.example`.

Missing keys or consent degrade that role to its deterministic fallback and record a gap (planner and implementers). A missing Jev key or consent silently degrades the decider to not-applicable — visible in the decide records and `doctor`. A run never fails for any of them. `doctor` warns when the layer is configured on without them, without echoing values.

Then run:

```powershell
.\.venv\Scripts\python.exe -m src.cli intel acme.com --with-llm
```

**Gates.** Jev answers typed questions at the stage boundaries. Every decision lands in a per-run `decisions.jsonl` beside the package. Each boundary also adds one aggregate evidence record (`kind: "jev_decision"`) to the dossier.

There are seven gates: five numbered, plus two unnumbered screens placed where they run in the pipeline. G1, G4, and G5 have floors of 0.70 noul — Jev's typed numeric score. Every threshold is configurable in `config/decide.yaml`.

| Gate | Boundary | What it does | On Jev error |
|---|---|---|---|
| G1 plan qualification | plan → collect | In enforce, keeps/drops each LLM plan step. | Step reverts to deterministic collection (never dropped from collection). |
| G2 posture audit | after collect | Shadow-only audit — logs, NEVER binds. The deterministic posture machinery is the gate. | n/a |
| G3 routing | per document | Deterministic token heuristic first. Jev breaks ties only (±20% of `route_threshold`). | Falls to the deterministic heuristic. |
| Document gate | before implementer spend | In enforce, screens each staged document with three questions: relevant to the account, contains citable evidence, contains prompt-injection instructions. Irrelevant, weak, or injected docs never reach the implementers. | Docs pass through un-screened. |
| G4 citation soundness | per document's claim batch | In enforce, deterministic prefilters run first, then a 3-way Choice routes each claim: supported → kept, contradicted → dropped, says-nothing or sub-floor probability → review. | LLM claims for that boundary are dropped. |
| Output screen | on the citation request | In enforce, two batch questions: do claims assert facts absent from the excerpt, and do they concern the wrong entity? The gate can hold the whole claim batch for review or drop it. Its decisions fold into the citation-soundness record. | Falls to the existing citation-gate error path. |
| G5 need promotion | implement → score | In enforce, `llm_need` candidates promote at noul ≥ the floor. Below the floor they land in the review band (`band_low`, default 0.30); below the band they drop. The probability is stored regardless. | The LLM-promoted need is dropped. |

**G4, in detail.** In enforce, two deterministic checks run first: doc-existence and lexical overlap. A verbatim quote-span check follows, matched after case, whitespace, and quote-mark normalization. A claim whose quote is not in the document dies at zero Jev cost, in every mode.

Each claim is judged against an excerpt built around its VERIFIED QUOTE — a
window around the span, with the claim text included in the question. Before
this fix, the excerpt was anchored on the claim's paraphrase instead. That
anchor landed 88–97% of claims against the document's boilerplate head rather
than the relevant passage (`data/probe/G4_WALL_2026_10.md`). With
`lab.persist_claims: true` in `decide.yaml`, G4 rows carry the judged claims
for offline diagnosis.

**Review outcomes.** Verdicts inside the review band (or a says-nothing Choice) are NEVER applied in any mode. In enforce, that claim or field does not land in this run. In shadow, nothing binds: the pre-review shadow behavior stands, and only the detail row records the would-be review.

The decision is always recorded — `reviewed` counts in the decide aggregates, detail rows in the dossier and `decisions.jsonl`. Floors can then be calibrated from real traffic instead of guesses (`src/decide/audit.py` ships the repeat-and-measure sampler). Review is bookkeeping, not a third mode.

**Enrichment passes.** Beyond the gates, the enforce path adds calibrated enrichment to candidates and the dossier. Every pass fails open to skip, with rows in `decisions.jsonl`. In shadow, the passes record only — nothing lands in the shadow dossier.

- **Completeness cascade** (`completeness_verify`) — after the five dossier fields are extracted, a per-field verify battery runs. Two checks can mark a field bad=TRUE: "the doc states it, so unknown is wrong" and "this fill is not supported by the doc". Routing is max-style: unsupported fills are quarantined. Unknown-but-supported fields escalate the document to the reasoning model for a re-extract, capped per run.
- **Taxonomy typing** (`taxonomy_typing`) — one Choice per accepted claim over the 54 signal types. Confidence ≥ 0.90 with a clear top/second separation proposes a type; a near-tie or lower confidence proposes only the parent category. Proposals ride `evidence_data` — the deterministic pipeline's `signal_type` is never changed.
- **Event dates** (`event_dates`) — for claim-bearing documents, seven Choice questions (mode/month/day/year/anchors). The CODE does all calendar math against the document's fetch date as the pinned reference. A low-confidence or unstated date records `event_at_needs_review` and nothing else. `observed_at` is unchanged.
- **Value extraction** (`value_extraction`) — a deterministic regex proposes money spans in accepted claims. Jev picks which span (if any) states the asserted amount and whether it is a raise rather than a valuation. Code copies the span verbatim and normalizes (`amount_usd`, currency; no FX conversion in v1). The model can never invent or transpose a digit.
- **Evidence re-rank** (`evidence_rerank`) — rides the document-gate request. A strength head orders the implement loop, strongest first. An optional `keep_n` caps how many documents get implementer calls at all.
- **Entity alignment** (`entity_alignment`) — runs in `sweep --discover` / `--competitors` (scope-guard v2 authorizes the discover frames; watch/scheduler remain excluded). The top candidate pairs get one request each: a three-level Score (different / related / same — routed by round-to-nearest-level, no thresholds) plus per-field noul scores. Queued candidates carry `jev_alignment` with the per-field disagreement evidence — exactly what the human review queue lacked. The deterministic apex auto-accept ladder is untouched.

**Fail rules.** The deterministic derive/package output is always produced in full. LLM content is additive on top. A Jev outage drops all LLM content, and the artifact says so: `decide_degraded: true` in `manifest.json` plus a gap line.

Dropped G1 steps still revert to deterministic collection. The planner-narrowed enforce scope itself stays narrowed — reverting the steps does not widen the plan back. Shadow mode never changes the applied path; it only logs. Narrow-only invariant: no gate verdict can add sources, budget, or posture.

**Cost.** Expect ≈ $0.15–0.30 per `intel` run at ~200 stored documents. The implementers dominate. The document gate cuts their spend by excluding irrelevant documents before any implementer call — each individual Jev decision is sub-cent.

The token budget is pre-flighted per run (`decider.max_decide_tokens_per_run`, default 400,000). Over the ceiling, the whole run degrades to shadow — it never aborts mid-run.

**Determinism caveat.** Run-to-run byte-identity and backtest reproducibility hold only at `mode: "off"`. With the layer on, outputs may differ between runs. The plan cache (`data/llm/plan_cache/`) and the decision ledger make re-runs explainable, not byte-identical.

## Identity discovery

`sweep --discover "Company Name"` resolves a bare company name to a domain
through a keyless waterfall. Discovery never creates accounts — the outcome is
a printed report plus a review queue:

1. **Wikidata** — the `P856` official website
2. **Wikipedia** — external links
3. **Google Knowledge Graph** — credential-gated, see below
4. **DuckDuckGo SERP** — only behind `--ddg`, the probe-gated robots exception
   owned at the top of this README

The Google Knowledge Graph source (`gkg_ids`) ships `enabled: false` in
`config/sources.yaml`. It is not a cadence adapter — the waterfall calls it at
resolve time only.

A run reports `resolved` on an unambiguous single-stage hit. Two stages
agreeing on the same root domain also resolve — the 2-source apex rule; e.g.
Wikidata `P856` == GKG url. The agreeing stages are printed. Any other outcome
is `ambiguous` or `no_match`. Either way, ranked candidates land in the
`identity_candidates` review queue (kind=domain; alternates from resolved runs
included), and `doctor` surfaces the pending count. Promotion is always human:
rerun `sweep <chosen-domain>` to onboard the account.

**GKG credentials.** Both GKG stages are silent no-ops (`status: unconfigured`)
until configured. The doctor's `gkg_credentials` check WARNs while they are:

- **EKG (default backend)** — enable the Cloud Enterprise Knowledge Graph API
  in the Google Cloud console and create a service account. Set
  `GOOGLE_APPLICATION_CREDENTIALS` (path to its JSON key) and `GKG_PROJECT_ID`,
  then install the optional extra:
  `.\.venv\Scripts\python.exe -m pip install -e ".[gkg]"` (google-auth). Bare
  API keys do not work on EKG — auth is a service-account OAuth2 bearer token.
- **Legacy fallback** — set `gkg_backend: kgsearch` in `config/default.yaml`
  and `GOOGLE_KGSEARCH_KEY` (a bare API key).

Only derived fields (name, url, entity id, domain) are ever persisted — never
raw Google payloads.

`sweep --competitors "Company Name"` mines comparison headlines into the same
queue as kind=competitor rows. The sources are Bing News RSS and HN Algolia,
both keyless. Promotion is a human edit: approved domains go into
`config/lists/competitors.txt` (drives the `icp.yaml` competitor
disqualifier), and/or names into `competitors:` in `config/fingerprints.yaml`
(drives `competitor_detected`).

The G2 competitors-page pass is deferred — it is DataDome-challenged. See
`data/probe/G2_COMPETITORS_2026_09.md`; it needs fresh cookies or solver keys.

## X-ray SERP prospecting (opt-in)

`xray` runs search-operator prospecting ("X-ray search"): a library of SERP
query strings pointed at a SERP engine. The strings combine five operators —
`site:`, quoted phrases, `OR` variants, minus exclusions,
`intitle:`/`inurl:`. They find people by title, and companies by their hiring
posts, funding phrases, and buyer-intent phrases.

This is the keyless route to "who is the VP Sales at a company we've never
scraped" and "which unknown companies are hiring the role our service
replaces." Those are the two discovery shapes the account-keyed fanout cannot
do.

```powershell
.\.venv\Scripts\python.exe -m src.cli xray --kind hiring --role "head of sales"
.\.venv\Scripts\python.exe -m src.cli xray --kind people --title "head of growth" --location "Austin" --limit 3
.\.venv\Scripts\python.exe -m src.cli xray --stats
```

- **Strings live in `config/lists/xray_strings.yaml`** — five families:
  `people_title_city`, `people_niche_keyword`, `hiring_post_role`,
  `recently_funded_niche`, `intent_problem_phrase`. "Swap the niche" = edit
  the file. The slots `{title} {location} {niche} {role} {problem_phrase}` are
  filled by the CLI options; a string with an unfilled slot is never fetched.
  `--kind` filters by string kind (`people`, `company`, `hiring`, `intent`;
  repeatable).
- **Never-guess outputs.** Company hits from company/hiring/intent strings
  queue ONE `identity_candidates` row per query (kind=domain, source=xray)
  for human review. Nothing auto-creates accounts, and known cohort roots are
  dropped before review. Profile hits from people strings become `contacts`
  rows only when the parsed company name casefold-matches a cohort account.
  Unmatched profiles are ledger-only.
- **Every query lands in `data/xray/ledger.jsonl`** — UTC, one row per query,
  status `ok`/`challenge`/`empty`/`error`. `xray --stats` aggregates per
  string id: run the strings that produce, drop the ones that don't.
- **The engine is stochastic.** The lite endpoint serves 202 anomaly
  challenges / 403 error-lites on some requests. Each query retries
  (`--attempts`, `--attempt-pause`) with pacing (`--pace`) and records the
  honest outcome; a challenge never parses as results. Budget: `--limit`
  queries per run.
- **Engines** (`--engine`, default from `config/default.yaml`
  `xray.default_engine`):
  - `ddg_lite` (default) — keyless, stochastic, thin yield on operator
    queries; the only probe-proven keyless path.
  - `google_state` — Google embeds its full organic result set as
    `window.W_jd` "2003" url+title state. The rendered hrefs are opaque
    `/goto?url=` blobs, so the state blob is the only parse source.
    Snippets are always empty — the records carry url+title only, which is
    what the company/profile extraction works from.
  - `google_state` cookies — the engine requires a fresh, browser-harvested
    cookie jar. Run `.venv\Scripts\python.exe scripts\google_cookie_refresh.py`
    before the session: solve, harvest `data/xray/google_cookies.json`, then
    replay. Cookies last one session (~24h max observed) — never replay an
    older jar; stale jars correlate with 429 captchas. Intermittent IP-level
    captcha walls serve explicit `challenge` ledger rows — never results. The
    robots-exception posture extends to this engine: manual, opt-in,
    self-paced, and it spends browser-minutes (the refresh script) plus a
    handful of paced replay fetches per session.
- **Robots exception + probe record.** This is the second documented robots
  exception (see the ethics paragraph above): manual invocation only, never
  scheduled, never a cadence fanout, self-paced, a handful of requests per
  run. Google probed NO-GO *keylessly* on every transport (the 200 JS-gate
  shell). But the 2026-10 bypass ladder proved the headed-browser /
  fresh-cookie-replay bodies carry full result sets as embedded state — hence
  the opt-in `google_state` engine above. Bing/Brave/Mojeek probed NO-GO
  (operators ignored / hard walls); SearXNG blocked on Docker. Full verdict
  matrix: `data/probe/XRAY_SERP_2026_10.md`.

## Email delivery

Email briefs and digests over stdlib SMTP: `digest --email`, `brief --email`.
Configure in `.env`:

```
SIGNALS_SMTP_HOST=smtp.example.com
SIGNALS_SMTP_PORT=587
SIGNALS_SMTP_USER_ENV=SIGNALS_SMTP_USER   # env var NAMES, not credentials
SIGNALS_SMTP_PASS_ENV=SIGNALS_SMTP_PASS
SIGNALS_EMAIL_TO=you@example.com
```

Credentials live in the env vars those names point at — resolved at send
time, never stored in config. Send failures are logged and never crash the
run; the digest/brief file is written either way.

## Form D funding

SEC private-offering notices. Pooled investment funds are excluded by default.
Unknown issuers land on stub domains like `cik0001234567.edgar`.

The `funding` command has three subcommands — `recent`, `search`, and
`company`. `funding company --domain radicl.com` fetches the homepage, peels a
legal name, searches Form D for the quoted name, and drops collisions
(Scanner ≠ Surgical Safety Scanner).

```powershell
.\.venv\Scripts\python.exe -m src.cli funding recent --days 30 --limit 100
.\.venv\Scripts\python.exe -m src.cli funding search "robotics" --days 365
.\.venv\Scripts\python.exe -m src.cli funding company --cik 0001234567
.\.venv\Scripts\python.exe -m src.cli funding company --domain radicl.com
.\.venv\Scripts\python.exe -m src.cli collect --source sec_formd --force
.\.venv\Scripts\python.exe -m src.cli export --what funding
```

## Sources

Enabled adapters live in `config/sources.yaml`. Each bullet below names one adapter and what it collects.

**SEC.**

- `sec_edgar` — EDGAR filings for the account's resolved CIK.
- `sec_formd` — Form D private-offering notices (see *Form D funding*).

**ATS.**

- `ats_greenhouse` — job postings from the account's Greenhouse board.
- `ats_lever` — job postings from the account's Lever board.
- `ats_ashby` — job postings from the account's Ashby board.
- `ats_smartrecruiters` — job postings from the account's SmartRecruiters board.
- `ats_workable` — job postings from the account's Workable board.
- `ats_recruitee` — job postings from the account's Recruitee board.
- `ats_workday` — job postings from the account's Workday board.
- `ats_rippling` — Rippling postings, on the same `ats_vendor` + `ats_token` contract.
- `ats_jobvite` — Jobvite postings, on the same `ats_vendor` + `ats_token` contract.
- `ats_breezy` — Breezy postings, on the same `ats_vendor` + `ats_token` contract.
- `ats_teamtailor` — Teamtailor postings, on the same `ats_vendor` + `ats_token` contract.
- `ats_careers_page` — a conservative `/careers` HTML fallback. It fires only for accounts with no ATS vendor or token.

All ATS sources read the account's `ats_vendor` + `ats_token`; the last four
(`ats_rippling` through `ats_teamtailor`) were added in the P2 wave on the same
contract.

**Making an ATS source fire.** Set the account's `ats_vendor` and `ats_token` — Feed Me CSV columns at seed time, or `deepen` for per-account updates. Once both are set, `ats_careers_page` stays quiet for that account (no duplicate job rows). It still covers accounts with neither field set.

**Careers-page detection.** Auto-detection is sitemap-first. `resolve --ats` and the sweep resolver pass both use it (`src/identity/sitemap_careers.py`). The resolver reads robots.txt `Sitemap:` directives, walks the sitemap (or sitemap index), and scores every URL for careers-index shape. Failing that, it hops homepage links, then tries hardcoded path guesses. A sitemap-discovered `careers_url` is persisted even when no ATS is detected, so `ats_careers_page` scrapes the real index instead of a guessed path.

Detection covers all 11 collected vendors. Collector-less boards (bamboohr, jazzhr, personio) deliberately set only `careers_url`, so the careers-page fallback keeps firing for those accounts.

**News / regulators.**

- `news_rss` — Google News search on the quoted account name, plus Bing News.
- `google_news` — keyword search RSS plus named topic sections (details below).
- `company_feed` — the account's own blog via `blog_feed_url` (product launches).
- `federal_register` — agency-rule watch over the Federal Register API.
- `warn_notices` — WARN Act layoff notices. NY and CA collect live; NJ/FL/OH parsers ship but are not in the fanout; WA/TX/IL are recon stubs; MI is deferred.

**How `google_news` works.** It fetches keyword search RSS (per-account name) plus named section feeds (TECHNOLOGY, BUSINESS — a class constant, not configurable). Everything runs through the same `classify_news` pipeline: funding, exec hires, M&A, product launches, security breaches. For higher recall, the same query runs again with signal keywords appended: fundraising, new leadership, new GTM product, acquisition. The list lives in `serp_keywords` in `config/sources.yaml`. Full notes: [`src/sources/news/README.md`](src/sources/news/README.md).

**Federal spend.**

- `federal_contracts` — live usaspending.gov award search (keyless POST). It uses a trailing-12-month window and never-guess name matching: ambiguous recipients emit nothing.

**Footprint.**

- `techstack` — observed third-party tech plus DNS/header evidence (details below).
- `wayback` — archived homepage/pricing diffs.
- `crtsh` — certificate-transparency subdomains; hint labels plus new-subdomain deltas.
- `jobsignals` — hiring trends derived from stored job postings.
- `needs` — derives profile-matched operational needs from first-party docs and explicit job stack demands.

**How `techstack` works.** It indexes observed third-party hosts from HTML and HAR-lite evidence; the YAML only *names* common platforms (HubSpot, Webflow, GTM, …). Unknown SaaS still lands as `host:cdn.example` in `technologies`, not as `tech_install_new`. After seed, backfill once with `.\.venv\Scripts\python.exe -m src.cli collect --source techstack --force`. Full notes: [`src/sources/techstack/README.md`](src/sources/techstack/README.md).

**Community.**

- `community_hn` — Hacker News mentions via the Algolia API.
- `community_github` — org repos and releases: `product_launch`, stagnation, `github_momentum` (new repos, star surges, archived repos).

**How `community_github` finds the org.** It authenticates with `GITHUB_TOKEN` from the environment when set — 5,000 req/hr instead of the unauthenticated 60. The org login is guessed from the domain label or account name; only the first guess is probed per cycle. A GitHub login that differs from both is a known gap.

**App stores / registry.**

- `appstore_reviews` — live iTunes RSS reviews plus version capture. Set the account's `app_store_id` (a seed CSV column or a `deepen` update).
- `content_itunes` — vendor podcast presence via the iTunes Search API.
- `bbb_profile` — live BBB business profiles; grade downgrades emit `reputation_drop`. Seed `extra_data.bbb_url` per account — those URLs are not derivable from name and domain.

**Local / opt-in DBs.**

- `owned_intent` — intent data you drop in locally; nothing is fetched from the network.
- `linkedin_db` — reads the companion LinkedIn scraper's local DB, read-only. Live collection stays manual via `deepen` (see `src/sources/linkedin_db/README.md`).
- `repvue_db` — an optional local sales-hiring DB; no live fetch.

**Disabled by default.**

- `community_reddit` — live fetching is blocked: a www 403 block page plus the old.reddit login wall (P2 spike 2026-08-31).
- `yc_batch` — the Y Combinator directory is a client-rendered Inertia shell; the parser needs a browser-tier upgrade.
- `content_producthunt` — Cloudflare-blocked.
- `marketplace_g2` — G2 reviews; a browser-tier DataDome target. Live runs need `DATADOME_SOLVER_PROVIDER`, `DATADOME_SOLVER_API_KEY`, and `DATADOME_RESIDENTIAL_PROXY` — see `.env.example`.
- `marketplace_capterra` — Capterra reviews; server-rendered HTTP behind Cloudflare.
- `marketplace_trustradius` — TrustRadius reviews; server-rendered HTTP behind Cloudflare.
- `marketplace_softwareadvice` / `marketplace_getapp` — the Gartner-network pair: server-rendered JSON-LD reviews, opt in via `extra_data.sa_url` / `extra_data.getapp_url`.

**Marketplace gating.** The adapter's `enabled` flag in `config/sources.yaml` is the enforced gate; all marketplace adapters ship `false`. The `sites.*` blocks in `config/marketplace.yaml` hold per-site options (cookie file, page limits). Setting a site's `enabled` there is documentation of intent, not an additional code-enforced gate. ToS restricts automation — full notes for G2, Capterra, and TrustRadius in `src/sources/marketplace/README.md`.

### Cloudflare bypass

When `techstack` (or a marketplace source) hits a Cloudflare challenge (403 or managed interstitial), a 6-step bypass waterfall attempts to solve it:

1. Reuse a cached `cf_clearance` cookie.
2. Send the request through the SignalsShadow native engine (when the antibot engine is built).
3. Solve the JavaScript challenge in headless Chromium.
4. Call an external solver: a 2Captcha Turnstile token. The token is re-injected via the browser as a `cf_clearance` cookie and polled for clearance.
5. Fall back to headed auto-solve (no manual interaction).
6. Stop honestly: the result names `cloudflare` and invents nothing.

Bypass is scoped to `techstack`, `marketplace_g2`, `marketplace_capterra`, and `marketplace_trustradius` (`_CF_BYPASS_SOURCES` in `src/pipeline/runner.py`). All other sources retain the 403 hard-stop. Cookies persist in `cloudflare_cookies`, bound to the user agent and proxy. Enable the solver in `.env`:

```
CLOUDFLARE_SOLVER_PROVIDER=2captcha
CLOUDFLARE_SOLVER_API_KEY=your_key
CLOUDFLARE_BYPASS_STRATEGY=browser_first
```

See [`src/sources/techstack/README.md`](src/sources/techstack/README.md) for the full waterfall and [`src/sources/techstack/cf_bypass.py`](src/sources/techstack/cf_bypass.py) for the implementation.

## Signals and the backtesting loop

P2 added signal families beyond news: `pricing_change` (wayback snapshots of
`/pricing` diffed each cycle — a plan/price change means budget is moving) and
`hiring_surge` (open-role deltas from the ATS/job-board sources, with both a
minimum percentage delta and an absolute-count floor so tiny boards don't
fire). The catalog holds **54 signal types** — newer additions:
`security_breach` (news-classified breach detection; maps to the
`trust_rebuild_pitch` play), `relocation` (BBB business-profile address
delta, fires only for accounts seeded with a `bbb_url`), and the core-source
insights families: `positioning_change` (archived-homepage title/meta diff),
`renewal_window` (vendor first-seen anniversaries), `github_momentum`
(new repos / star surges / archived repos), `funding_drought` (18–24 months
silent after a Form D raise), `federal_contract_award` (usaspending.gov),
`new_subdomain` (crt.sh cycle-over-cycle), `bankruptcy_signal` /
`contract_terminated` (8-K items 1.03/1.02), `insider_trade` (Form 4),
`competitor_outage` (direct statuspage.io polling), `reputation_drop` (BBB
grade downgrade), `tech_removed` / `competitor_detected` / `backfill_open`
(now all wired), and mail-vendor churn from DNS evidence (`tech_churn` on
removed SPF includes), plus three operational-need signals — `need_statement`
and `required_stack_demand` from the `needs` derive pass, and LLM-gated
`llm_need` (promoted only through the G5 gate above). Entity resolution
(`normalize_entity` + fuzzy match, `config`
`entity_aliases`) ties same-company variants across sources to one account,
and `config/icp.yaml` scores accounts at seed time so tiering is real from
day one.

### Relevance reranking (enabled)

Google News matches on company name are surface-level: an `"Acme" CEO` SERP
query returns tangential coverage that keyword guards can't fully judge. The
cross-encoder reranker scores every SERP candidate for true semantic
relevance, drops below-floor matches before classification, and stamps
survivors with `evidence_data["relevance"]` (which then decides which same-day
candidates survive the per-type cap). **Shipped enabled** in
`config/default.yaml` (`rerank.enabled: true`, floor 0.35) with the
`signals[rerank]` extra installed:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rerank]"
# rerank.enabled is already true in config/default.yaml (floor 0.35)
```

Enablement note: with `enabled: true` but the extra not installed, the reranker
degrades silently to `NullScorer` (log line: "rerank model unavailable,
reranking disabled: ...") — the pipeline never fails, but check the log line
if you expect ranking and see none.

Implementation: `ms-marco-MiniLM-L-6-v2` cross-encoder (22M params, Apache-2.0)
via ONNX Runtime on CPU (~1-4ms/pair; a full SERP pool costs ~0.1-0.3s per
account), model revision pinned in `src/signals/rerank.py`, session memoized
per process. If the model can't download or inference fails, that feed falls
back to unranked with a logged warning — reranking is an enhancement, never a
dependency.

The play-outcome loop closes the calibration circuit:

```powershell
# after runs, record what happened to the plays you worked:
.\.venv\Scripts\python.exe -m src.cli plays --outcome hit --domain acme.com --play growth_pitch
.\.venv\Scripts\python.exe -m src.cli plays-report       # per-play sent/hit/rate
.\.venv\Scripts\python.exe -m src.cli plays-calibrate    # feed decided outcomes into calibration
```

Once ≥30 decided outcomes exist for a (source, signal_type), `blend_confidence`
starts adjusting candidate confidence from real hit rates. Digests are the
read-side: `digest --period daily` writes per-account markdown (grouped by
signal type with why-now lines) to `data/digests/`; add `--email` to send it.

An account health score (polarity of its signals — a WARN + layoffs account
scores negative) gates growth-family plays automatically, so you don't pitch
growth into a layoff. Defaults are fine; tune via `config/health.yaml` and the
`health_gate` block in `config/plays.yaml` (add the key to override the
code defaults: threshold −0.5, suppressed family `growth_pitch`).

## Anti-Bot Evasion

`src/antibot` (SignalsShadow) is a native Rust/BoringSSL fetch tier whose TLS
fingerprint is emergent from Chrome's own engine — "Chrome TLS, not
Chrome-like" — plus an in-house HTTP/2 stack (Akamai h2 byte-exact), temporal
stealth (session resumption, pooling, 304 revalidation), and solve-and-bounce
ghost orchestration with per-domain routing that learns clearance-cookie
lifetimes (RouteState, `data/antibot/routing.json`). Falls back to
`curl_cffi` when the engine isn't built. The antibot module itself does no
CAPTCHA solving and no login bypass (the external-solver Cloudflare/DataDome
tiers live in the source fetchers — see *Cloudflare bypass* above) — honest
limits are documented. Full notes: [`src/antibot/README.md`](src/antibot/README.md).

## Decide lab (dev tooling, opt-in)

`src/lab` is a dev-only package for tuning the decide layer's gates against
the decision ledgers intel already collects — the TypeSafe
`autoresearch_feature_discovery` shape: propose candidate gate questions →
4-Noul prescreen → correlation prune → replay against recorded corpus states
under a weighted score (acceptance precision / claim recall / promotion
proxy / cost / latency) → supervised comparison (elastic-net/RF/SVR/HGB +
the deterministic + MiniLM-embedding baselines) → errors feed the next
round, with chronological + domain-grouped folds, a final holdout, plateau
stopping, and stability checks. It never runs inside `intel` (an
import-graph guard enforces the isolation) and its report PROPOSES
`config/decide.yaml` diffs — applying them is a human step.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[lab]"
.\.venv\Scripts\python.exe -m src.lab corpus                     # stats only, no writes
.\.venv\Scripts\python.exe -m src.lab run --rounds 3 --budget-calls 150 --dry-run
.\.venv\Scripts\python.exe -m src.lab run --rounds 3 --budget-calls 150 --target claim_survival
```

Live rounds need the same TypeSafe/OpenRouter credentials + data-exit
consent as the decide layer (skipped honestly without them). The measured
opening targets: G5's `band_low` (all 49 G4-accepted claims in the fix-wave
validation died at promotion nouls 0.08–0.13 vs the 0.30 band), the batch
output screen (`output_review` is the dominant reviewer post-fix), and the
`taxonomy_typing` 422s — see
[`data/probe/G4_WALL_2026_10.md`](data/probe/G4_WALL_2026_10.md).

## Scheduling, retention, and calibration (ops)

- **Durable scheduling** — `watch` runs on a `Scheduler` (`src/pipeline/scheduler.py`):
  per-source cadence from `config/sources.yaml`, missed-run catchup, ±5%
  jitter, failure backoff (cadence ×2 per consecutive failure, capped 8×), and
  a single-flight lockfile (`data/state/collect.lock`, atomic create, stale
  lock auto-break). OS cron invoking `collect` per source remains the
  recommended production setup.
- **Retention** — `prune --keep-days N` deletes expired `fetch_log` /
  `documents` / `runs` rows and raw-store files, then WAL-checkpoints and
  ANALYZEs. Add `--vacuum` to reclaim disk.
- **Confidence calibration** — a `calibration` table (schema migration v2)
  stores per-source/per-signal-type hit rates; `blend_confidence` adjusts
  candidate confidence once ≥30 samples exist. The loop is now closed: record
  play outcomes with `plays --outcome`, then `plays-calibrate` feeds decided
  outcomes into the table (P2 backtesting, migration v5).
- **DB migrations** — schema changes run as ordered, transactional migrations
  tracked by `PRAGMA user_version` (currently v9), with a startup
  `integrity_check`.
- **Account health gate** — a pure polarity score (`src/signals/health.py`,
  weights in `config/health.yaml`) suppresses growth-family plays for
  negative-signal accounts (default threshold −0.5; `health_gate` in
  `config/plays.yaml`).
- **Signed webhooks** — beyond the Slack channel, `ALERT_WEBHOOKS_JSON`
  configures generic JSON webhooks with optional HMAC signing (secret
  referenced by env-var name, resolved at send time). Each entry takes a
  per-webhook `signature` option: `legacy` (`X-Signature: sha256=<hex>`,
  default), `hub` (`X-Hub-Signature-256: sha256=<hex>`), or `stripe`
  (`Stripe-Signature: t=<unix-seconds>,v1=<hex>`); unknown values fall back to
  `legacy` with a warning.
- **Per-tier routing** — `ALERT_ROUTES_JSON` (a JSON array of
  `{min_tier, max_tier, webhooks, digest?}`) routes alerts by account tier to
  specific webhooks or digest-only; absent = all alerts to all webhooks.
- **Export destinations** — `exports.destinations` in `config/default.yaml`
  (default `[{type: file}]`) configures the fan-out; add
  `{type: webhook, ...}` for signed alert-webhook delivery via the same path,
  or `{type: slack}` — the same webhook destination with Slack payloads pinned,
  for Slack-app incoming webhooks (self-contained install flow; the webhook URL
  is itself the revocable secret; channel and icon are fixed in the Slack app
  config and not overridable per message).
- **Raw-store quota** — set `storage.raw_quota_mb` in `config/default.yaml` to
  cap `data/raw` disk usage (doctor/status WARN when exceeded; off by default).

## Add a source in 20 lines

1. Write a pure `parse_*(body) -> list[SignalCandidate]` (no I/O, no clock).
2. `@register` a `SourceAdapter` with `plan` + `parse`.
3. Import the module in `src/sources/__init__.py` — `@register` only populates
   `SOURCES` if the module is imported there (missing this = silent no-op).
4. Enable it in `config/sources.yaml`.
5. Drop a fixture under `tests/fixtures/<source>/` and a test.

Marketplace-tier sources (browser/HTTP fetcher wiring, a
`config/marketplace.yaml` site entry, a selector-drift selfcheck) are
substantially more involved — see `src/sources/marketplace/README.md`.

## Legal / ethics

- Respect `robots.txt` unless you deliberately turn it off for a run.
- Identify yourself. Set `SIGNALS_CONTACT_EMAIL` — SEC 403s a missing contact.
- No auth bypass, no paywall circumvention, no personal non-work data.
- Cloudflare and DataDome bot-challenge bypass is scoped to `techstack`,
  `marketplace_g2`, `marketplace_capterra`, and `marketplace_trustradius`
  (public business/review pages). It solves JS/managed challenges (external
  Turnstile/DataDome solver tiers) to read that
  content — it does not bypass authentication, paywalls, or login-gated
  content.
- G2 / Capterra / TrustRadius / LinkedIn ToS restrict automation — those
  adapters stay disabled by default (opt-in via the `enabled` gate above).
- Never resell raw content. The raw store is a local reproducibility cache.

## First-run checklist

- [ ] What do you sell? (ICP, competitors, play `{your_product}`)
- [ ] Which analytics/ESP? (owned-intent column mapping)
- [ ] Geography focus? (8 WARN state parsers ship — NY/CA/WA/TX/IL/NJ/FL/OH;
  MI deferred, needs a browser tier)
- [ ] Champions list?
- [ ] Alert destination? Slack webhook (`ALERT_WEBHOOK_URL`), signed JSON
  webhooks (`ALERT_WEBHOOKS_JSON`), per-tier routes (`ALERT_ROUTES_JSON`),
  email (`SIGNALS_SMTP_*`), or file-only (fan-out via `exports.destinations`).

Deferred: see the **Deferred — not scheduled** section in `TODO.md`
(Google Trends, ASN IP→org, CRM write-back, multi-user server, and others).

Release history: see [CHANGELOG.md](CHANGELOG.md) — current release **v0.2.0**.

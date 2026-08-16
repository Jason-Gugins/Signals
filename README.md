# Signals

Sales signal enrichment engine. Continuously collects buying signals about
target accounts from ~20 self-built, **zero-cost** sources, resolves them to a
single account identity graph, scores and tiers them per High Probability
Prospecting (HPP), maps each signal to a sales play, and exports ranked
account briefs.

No paid APIs. No ZoomInfo, Apollo, Exa, BuiltWith, or Bombora. Every capability
those tools provide is replaced by a local collector (SEC EDGAR, ATS JSON
boards, RSS, Federal Register, WARN, DNS/HTTP fingerprints, Wayback CDX,
HN/GitHub, owned-property ingest, plus read-only adapters over the existing
LinkedIn and RepVue scrapers).

## Install

```powershell
cd C:\Users\Jason\Documents\AI\Signals
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
# set SIGNALS_CONTACT_EMAIL to a real address (required by SEC EDGAR)
```

## The five commands a human actually runs

```powershell
# 1. Create the SQLite DB + data/ directories
.\.venv\Scripts\python.exe -m src.cli init

# 2. Load accounts from CSV and/or the LinkedIn + RepVue DBs
.\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv --from-linkedin --from-repvue

# 3. Collect, parse, score, and export a cohort
.\.venv\Scripts\python.exe -m src.cli run --cohort canada-software

# 4. Render a one-page brief for one account
.\.venv\Scripts\python.exe -m src.cli brief acme.com

# 5. Incremental watch loop (poll due sources, alert on new primary triggers)
.\.venv\Scripts\python.exe -m src.cli watch
```

Outputs land in `data/exports/`, `data/briefs/`, and `data/alerts/`.

## Architecture

`seeds → identity resolution → per-source collectors (raw store) → pure
parsers → normalized signals → decay-weighted scoring + stacking → HPP
tiering → play mapping → briefs/CSV/alerts`.

Fetch and parse are separated. Every byte fetched is content-addressed under
`data/raw/`. `signals reparse` re-derives signals with zero network calls.

## Config

Runtime knobs live in `config/`. Signal taxonomy, scoring combos, play
templates, ICP rules, and tech fingerprints are all YAML — no code change
required to retune a weight or disable a source.

## License / use

Internal sales-research tooling. Polite HTTP, robots.txt honored on the HTTP
tier, browser tier opt-in and off by default.

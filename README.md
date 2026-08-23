# Signals

Sales signal enrichment engine. Continuously collects buying signals about
target accounts from ~20 self-built, **zero-cost** sources, resolves them to a
single account identity graph, scores and tiers them per High Probability
Prospecting (HPP), maps each signal to a sales play, and exports ranked
account briefs.

No paid APIs. No ZoomInfo, Apollo, Exa, BuiltWith, or Bombora.

## Zero-cost doctrine

Public, business-relevant data only. Polite HTTP (`robots.txt` honored by
default). A real contact address in the User-Agent. Rate limits are floors.
Marketplace / LinkedIn collection is opt-in and off by default.

## Install

```powershell
cd C:\Users\Jason\Documents\AI\Signals
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
# set SIGNALS_CONTACT_EMAIL to a real address (required by SEC EDGAR)
```

## Init and file drops

```powershell
.\.venv\Scripts\python.exe -m src.cli init
```

A human must provide:

- `config/lists/champions.csv` — prior buyers (unlocks `champion_migration`)
- `config/lists/exclusions.txt` — domains to skip
- `config/lists/email_patterns.csv` — only if a pattern is *known*
- `data/inbox/owned/*.csv|*.jsonl` — first-party intent (web/ESP export)

## The five commands

```powershell
.\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv --linkedin --repvue
.\.venv\Scripts\python.exe -m src.cli run --cohort canada-software
.\.venv\Scripts\python.exe -m src.cli brief --domain acme.com
.\.venv\Scripts\python.exe -m src.cli export --format csv
.\.venv\Scripts\python.exe -m src.cli watch --once
```

Also useful: `signals status`, `signals doctor --no-network`, `signals reparse`.

Form D funding tracker (SEC private-offering notices). Pooled investment funds
are excluded by default. Unknown issuers land on stub domains like
`cik0001234567.edgar`. `--dry-run` goes before the subcommand.

`funding company --domain radicl.com` fetches the homepage, peels a legal name,
quoted Form D search, and drops name collisions (Scanner ≠ Surgical Safety Scanner).

```powershell
.\.venv\Scripts\python.exe -m src.cli funding recent --days 30 --limit 100
.\.venv\Scripts\python.exe -m src.cli funding search "robotics" --days 365
.\.venv\Scripts\python.exe -m src.cli funding company --cik 0001234567
.\.venv\Scripts\python.exe -m src.cli funding company --domain radicl.com
.\.venv\Scripts\python.exe -m src.cli collect --source sec_formd --force
.\.venv\Scripts\python.exe -m src.cli export --what funding
```

Outputs land in `data/exports/`, `data/briefs/`, and `data/alerts/`.
Raw bytes live in `data/raw/<xx>/<sha>.gz` (content-addressed gzip).

## Add a source in 20 lines

1. Write a pure `parse_*(body) -> list[SignalCandidate]` (no I/O, no clock).
2. `@register` a `SourceAdapter` with `plan` + `parse`.
3. Enable it in `config/sources.yaml`.
4. Drop a fixture under `tests/fixtures/<source>/` and a test.

## Legal / ethics

- Respect `robots.txt` unless you deliberately turn it off for a run.
- Identify yourself. SEC 403s a REPLACE_ME UA.
- No auth bypass, no paywall circumvention, no personal non-work data.
- G2 / Capterra / LinkedIn ToS restrict automation — those adapters stay disabled.
- Never resell raw content. The raw store is a local reproducibility cache.

## Open questions

1. What do you sell? (ICP, competitors, play `{your_product}`)
2. Which analytics/ESP? (owned-intent column mapping)
3. Geography focus? (WARN jurisdictions, regulators)
4. Do you have a champions list?
5. Alert destination? Slack webhook or file-only.

Deferred: Google Trends, ASN IP→org, CRM write-back, multi-user server.

# Marketplace Sources

G2 review scraper. Browser-tier, opt-in. Reuses the techstack Cloudflare bypass.

## G2

### Enable

1. Set `g2_slug` on the account (manual seed or CSV):
   ```powershell
   .\.venv\Scripts\python.exe -m src.cli seed --csv seeds.csv
   # seeds.csv: domain,name,g2_slug
   # acme.com,Acme,acme-crm
   ```

2. Enable in `config/sources.yaml`:
   ```yaml
   marketplace_g2:
     enabled: true
     cadence_hours: 168
     rate_per_host: 0.5
   ```

3. Enable browser tier in `config/default.yaml`:
   ```yaml
   browser:
     enabled: true
     headless: true
   ```

4. Collect:
   ```powershell
   .\.venv\Scripts\python.exe -m src.cli collect --source marketplace_g2 --force
   ```

5. Export:
   ```powershell
   .\.venv\Scripts\python.exe -m src.cli g2-export --slug slack
   # -> data/exports/g2/slack.json + data/exports/g2/slack.csv
   ```

### Cloudflare

G2 is behind Cloudflare. The existing techstack bypass waterfall is reused:
cookie reuse -> browser solve -> solver -> headed -> hard stop.
See `src/sources/techstack/README.md` for the full waterfall.

The bypass is gated to `techstack` and `marketplace_g2` sources via
`_CF_BYPASS_SOURCES` in `src/pipeline/runner.py`.

### Config

G2-specific options live in `config/marketplace.yaml` under `sites.g2`:

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch |
| `deep_reviews` | `false` | Click "Show More" on each review (slow, browser tier) |
| `sign_in_required` | `false` | Full review text requires G2 sign-in (manual cookie) |
| `max_review_pages` | `5` | Cap pagination |
| `review_lookback_days` | `90` | Drop reviews older than this |

### Storage

Reviews persist to the `g2_reviews` SQLite table (one row per review,
natural key = `sha256(product_slug|reviewer_name|posted_at)[:16]`).

Export formats:
- JSON: `data/exports/g2/{slug}.json` - list of review dicts (best for DB/API).
- CSV: `data/exports/g2/{slug}.csv` - one row per review (best for spreadsheets).

### Parser

`parse_g2_reviews(html, url) -> list[G2Review]` is pure (no I/O, no clock).
Extracts: reviewer name, title, company size, rating (from `stars-{N}` class),
review title, body, pros/cons, date, verified status, review source.

### ToS

G2 ToS restrict automated scraping. The adapter is disabled by default.
Enable at your own risk. See README Legal section.

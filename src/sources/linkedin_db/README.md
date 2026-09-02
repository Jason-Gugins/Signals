# linkedin_db — read-only adapter over the companion LinkedIn scraper

This adapter consumes the output of the companion LinkedIn scraper
(**github.com/Jason-Gugins/linkedin-scraper**, private; local checkout
`../Linkedin`) **read-only**. It never writes the scraper's database and never
touches LinkedIn itself.

## What runs when

- **`linkedin_db` cadence harvest (every 6h, automatic)** — opens
  `../Linkedin/data/linkedin.db` (config: `external_dbs.linkedin_db`) via a
  read-only connection and converts `people` / `jobs` / `posts` rows for the
  account's `linkedin_slug` into Signals candidates via
  `jobs.py` / `people.py` / `posts.py`. If the checkout or DB is missing, the
  harvest silently returns `[]` — a no-op, not a failure.
- **`deepen` (manual only)** — `python -m src.cli deepen --domain <domain>
  [--max-people N] [--timeout S]` runs the scraper's own CLI (`extract
  --company-url https://www.linkedin.com/company/<slug>/`) as a subprocess,
  writing output to `data/logs/deepen_<domain>.log`. This is the only path
  that touches LinkedIn live, and it is **manual-trigger by design** — the 6h
  cadence reads the local DB only.

## Requirements for live deepen runs

1. The scraper checkout at `external_dbs.linkedin_cli_cwd` (default
   `../Linkedin`) with its own venv (`.venv/Scripts/python.exe`).
2. A fresh session cookie: sign in manually in the scraper
   (`python -m src.cli login` in `../Linkedin`) — this persists
   `data/state/session.json`. **Expected to expire** (LinkedIn sessions are
   weeks, not months); refresh by signing in again. There is no automated
   login by design — automating LinkedIn auth is the highest ban-risk vector.
3. Optional: the scraper's Rust egress proxy (see `../Linkedin/egress-proxy/`).
   The 4.9GB `target/` directory is cargo build artifacts, not source — rebuild
   with `cargo build` if needed. Signals never needs the proxy.

## Honest caveats

- **Ban-detection unknown**: the scraper has passed bot checks in manual use,
  but there is no long-run agentic evidence. Treat LinkedIn scraping as a
  manual-effort, accept-the-risk source. If the account is ever banned, only
  the manual deepen workflow is affected — the DB harvest keeps working on
  historical rows.
- **Two databases, two schemas — deliberate.** The converters in this package
  are the only translation layer. The scraper is never imported by Signals
  (subprocess only), so its Playwright/antibot stack stays fully independent.

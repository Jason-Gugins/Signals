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
  --url https://www.linkedin.com/company/<slug>/`) as a subprocess,
  writing output to `data/logs/deepen_<linkedin_slug>.log`. This is the only path
  that touches LinkedIn live, and it is **manual-trigger by design** — the 6h
  cadence reads the local DB only.

## Signals emitted

- **`exec_hire`** — a profiled person's CURRENT role (from the `experience`
  JSON the scraper writes per profile) started within `max_role_months`
  (default 6) and the title parses as leadership (c-level/vp/director/head).
  Role dates AND title are read from the experience JSON's current entry —
  the flat `experience_dates`/`job_title` columns are listing-scrape only.
  Requires **profile scraping** (the scraper's `profile` command or a deepen
  that includes profiles); listing rows carry no role history.
- **`champion_migration`** — an exec_hire whose person is a seeded champion
  (`config/lists/champions.csv`) with a prior domain — the person you sold to
  at company A just landed at company B.
- **`leadership_job_open`** — a leadership-titled job posting listed within
  45 days (non-leadership postings are filtered by seniority).
- `people_to_contacts` also feeds champions into the contact layer
  regardless of signals.

## Requirements for live deepen runs

1. The scraper checkout at `external_dbs.linkedin_cli_cwd` (default
   `../Linkedin`) with its own venv (`.venv/Scripts/python.exe`).
2. A fresh session cookie: sign in manually in the scraper
   (`python -m src.cli login` in `../Linkedin`) — this persists
   `data/state/session.json`. **Expected to expire** (LinkedIn sessions are
   weeks, not months); refresh by signing in again. There is no automated
   login by design — automating LinkedIn auth is the highest ban-risk vector.
   The scraper's `status` command reports session age (WARN past 30 days).
3. Optional: the scraper's Rust egress proxy (see `../Linkedin/egress-proxy/`).
   The 4.9GB `target/` directory is cargo build artifacts, not source — rebuild
   with `cargo build` if needed. Signals never needs the proxy.

## Honest caveats

- **Ban-detection is real**: the first account used with this scraper was
  restricted by LinkedIn days after an aggressive multi-company run. The
  scraper now enforces a per-run company budget (default 5) and humanized
  behavior (scroll/dwell/typing) — treat LinkedIn as a manual-effort,
  accept-the-risk source. If the account is ever banned, only the manual
  deepen workflow is affected — the DB harvest keeps working on historical
  rows.
- **`linkedin_slug` must match exactly**: `deepen --domain X` looks up the
  account by domain and scrapes `linkedin.com/company/<linkedin_slug>/` — a
  wrong or domain-shaped slug (e.g. `databricks.com` instead of `databricks`)
  scrapes the wrong page or fails.
- **exec_hire needs profiles**: listing scrapes record people without role
  history; run the scraper's `profile` command (or a deepen that includes
  profiles) for a person before their role changes can be detected.
- **Two databases, two schemas — deliberate.** The converters in this package
  are the only translation layer. The scraper is never imported by Signals

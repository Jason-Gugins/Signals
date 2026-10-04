# Local exclusion / champion lists. The directory is gitignored, but the files below ship tracked in the repo — anything else you add here stays untracked.

Drop these files here (one entry per line, `#` comments allowed):

- competitors.txt   — domains we should never score as prospects
- customers.txt     — existing customers (route to CS)
- dnc.txt           — do-not-contact
- champions.csv     — known buyers / users (the Golden Trigger)
- xray_strings.yaml — the X-ray SERP query-string library used by `src.cli
  xray` (ships tracked; entries validated by `src/sources/xray/library.py`;
  string kinds: `people`, `company`, `hiring`, `intent`). "Swap the niche" =
  edit this file — the five string families and slot mechanics are documented
  in the root README's *X-ray SERP prospecting* section.
- entity_aliases.yaml — human-curated company-name aliases as
  {alias: canonical domain} (ships tracked; seeded with
  `"Abnormal Security": abnormal.ai`). Loaded into the registry at resolve
  time via `load_entity_aliases_from_config` (idempotent upserts) so
  former-brand tokens reach the ATS board ladder. Human-gated like all
  entity_aliases — never auto-derived; add a row by editing this file.
  The config file wins over manual DB edits: it is re-applied on every
  resolve.

The three `.txt` lists ship as comment-only stubs — edit them in place.
`champions.csv` does not ship; create it (and it stays untracked).

champions.csv header:

```
name,linkedin_slug,prior_company,prior_domain,relationship,last_touch,notes
Jane Doe,jane-doe,Gong,gong.io,former AE,2026-01-15,used us at Gong
```

A champion row needs `name` AND (`linkedin_slug` OR `prior_domain`).

Load the champion CSV (the only file with a loader command):

```powershell
.\.venv\Scripts\python.exe -m src.cli champions --load config/lists/champions.csv
```

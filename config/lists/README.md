# Local exclusion / champion lists. This directory is gitignored except this README.

Drop these files here (one entry per line, `#` comments allowed):

- competitors.txt  — domains we should never score as prospects
- customers.txt    — existing customers (route to CS)
- dnc.txt          — do-not-contact
- champions.csv    — known buyers / users (the Golden Trigger)

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

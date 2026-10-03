# Local exclusion / champion lists. The directory is gitignored, but these files ship tracked in the repo — anything else you add here stays untracked.

Drop these files here (one entry per line, `#` comments allowed):

- competitors.txt  — domains we should never score as prospects
- customers.txt    — existing customers (route to CS)
- dnc.txt          — do-not-contact
- champions.csv    — known buyers / users (the Golden Trigger)
- xray_strings.yaml — the X-ray SERP query-string library used by `src.cli xray`
  (ships tracked). Five string families: `people_title_city`,
  `people_niche_keyword`, `hiring_post_role`, `recently_funded_niche`,
  `intent_problem_phrase` (kinds people/company/hiring/intent). "Swap the
  niche" = edit this file; `{title} {location} {niche} {role}
  {problem_phrase}` are slots filled by CLI options and a string with an
  unfilled slot is never fetched; entries are validated by
  `src/sources/xray/library.py`.

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

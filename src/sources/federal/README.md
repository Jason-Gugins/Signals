# Federal contracts (usaspending.gov)

One Signals source (`federal_contracts`): live federal award search for each
account via the keyless usaspending.gov JSON API. No paid API, no anti-bot —
the P3 spike (2026-09-04, `scripts/source_spike_2026_09.py`, evidence in
`data/probe/P3_SOURCE_SPIKE_2026_09.md`) verified the endpoints live.

> Part of the [Signals](../../README.md) package — the root README covers
> clone/install (`pip install -e .`), `seed`, and the full CLI.

## How it works

`plan()` builds ONE POST task per account:

- endpoint `POST https://api.usaspending.gov/api/v2/search/spending_by_award/`
- body: `filters.keywords = [account.name or domain label]`,
  `award_type_codes = ["A","B","C","D"]` (contracts), a trailing-12-month
  `time_period` window, `limit: 100`
- the date window is built by `usaspending.build_search_body`, which is the
  ONLY clock read (that module defines no `parse`, so the AST purity guard
  does not scan it; `plan()` itself stays clock-free)

`parse()` normalizes the response rows (`usaspending.parse_awards`) and runs
each row's recipient name through the **never-guess ladder**
(`usaspending.match_recipient`): casefolded exact match or unique
bidirectional substring → match; anything ambiguous → NO candidate (an award
is never pinned on the wrong account). Matching rows emit
`federal_contract_award` (natural key `federal:{domain}:{award_id}` —
permanent, idempotent across cycles; evidence carries recipient, humanized
amount, period, description).

## Run

Account must be seeded first. Enabled by default (weekly, 168h):

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source federal_contracts --force
```

## Layout

| File | Role |
|---|---|
| `usaspending.py` | Pure helpers: request-body builders, `parse_awards`, `match_recipient` (never-guess ladder), `humanize_amount`; the sole `date.today()` default lives here (documented purity carve-out) |
| `collector.py` | `FederalContractsSource` — plan/parse, `@register`ed; requires no account fields beyond a name |

Tests: `tests/test_usaspending.py` (body shape, parsing, the full
match ladder including the ambiguous-skip case, persistence through
`normalize_batch`); fixture trimmed from the live spike capture
(`tests/fixtures/federal/awards_search.json`).

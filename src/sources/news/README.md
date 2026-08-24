# News Collector

Company-news signal engine. One Signals package (`src/sources/news`) behind three sources — **`news_rss`**, **`google_news`**, and **`company_feed`** — that turn press + blog coverage into classified sales signals (funding, exec changes, M&A, layoffs, launches). No paid news API.

The signal layer is a **rule-based classifier** (`classify.py`) with **attribution guards** that reject false positives where a company name appears as the publisher/author rather than the subject.

## Quickstart

Prereqs: Python 3.11+, a `Signals` checkout with `.venv` created (`python -m venv .venv` + `pip install -r requirements.txt`), and an account seeded (`python -m src.cli seed ...`). Run from the repo root. **Enable the sources + set `blog_feed_url` for `company_feed` in `config/sources.yaml`**, then:

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source news_rss --force
.\.venv\Scripts\python.exe -m src.cli collect --source google_news --force
.\.venv\Scripts\python.exe -m src.cli collect --source company_feed --force
```

## Sources

| key | What it gets | Host(s) | Cadence |
|---|---|---|---|
| `news_rss` | Google News search (quoted company name, 30d) + Bing News | `news.google.com`, `www.bing.com` | 12h |
| `google_news` | Google News **keyword search + SERP-augmented searches + topic sections** | `news.google.com` | 12h |
| `company_feed` | The account's own blog/`blog_feed_url` (product launches) | dynamic | 24h |

Enable/disable in `config/sources.yaml`:

```yaml
sources:
  news_rss:   { enabled: true, cadence_hours: 12, rate_per_host: 0.5 }
  google_news:
    enabled: true
    cadence_hours: 12
    rate_per_host: 0.5
    serp_keywords: [fundraising, series, new leadership, CEO, product launch, launches, GTM, acquisition, acquires]
  company_feed: { enabled: true, cadence_hours: 24, rate_per_host: 1.0, requires: ["blog_feed_url"] }
```

The account must be seeded first (see the main `Signals` README), and **`company_feed` requires `blog_feed_url`** set on the account.

## Google News search URL building

`feeds.py` builds RSS URLs for three feed types (the `ceid` component uses `{country}:{lang.split('-')[0]}` — `en-US` → `US:en`):

1. **Keyword search** — `https://news.google.com/rss/search?q={query}&hl={lang}&gl={country}&ceid={country}:{lang.split('-')[0]}` with a `when:{days}d` window (default 30d):

   ```
   google_news_search_url('"Acme Corp" fundraising')  ->  ?q=%22Acme+Corp%22+fundraising+when:30d
   ```

2. **Named section / topic** — `https://news.google.com/rss/headlines/section/topic/{TECHNOLOGY|BUSINESS|...}` or a raw topic ID via `/rss/topics/{id}`:

   ```
   google_news_topic_url("TECHNOLOGY")
   ```

3. **Bing News (used by `news_rss`)** — `bing_news_url(name)` → `https://www.bing.com/news/search?q={name}&format=rss`.

`parse_feed(body)` handles RSS/Atom via `feedparser`. Google News **redirect links unwrap** to the real publisher URL (`_unwrap`), so the stored signal points at the source article, not `news.google.com/...`.

### SERP manipulation (`google_news`)

Beyond the plain `"CompanyName"` query, `google_news` runs one **keyword-augmented search per `serp_keywords` entry** — e.g. `'"Levitate" fundraising'`, `'"Levitate" acquisition'` — to raise recall of on-topic coverage that a bare name search might bury. Configured in `sources.yaml` → `serp_keywords` and loaded by `serp_config.py` (mirrors the `jobsignals` thresholds loader). Duplicate articles are de-duplicated downstream by `natural_key` (the first 16 hex chars of the sha256 of the canonical link).

## How a signal is classified

`classify_news(item, account, today)` in `classify.py`:

1. **Attribution guards** (reject false positives — this is the hardened classifier):
   - **Source-attribution strip** — strips `"Headline - Publisher"` from Google News titles so a company appearing *only* as the byline isn't treated as a mention.
   - **Common-word guard** — for names that are also English words (`levitate`, `slack`, `stripe`, … in `_COMMON_WORD_NAMES`), requires a proper-noun/domain mention and rejects non-company contexts ("Levitate Music Festival", "Levitate #9" artwork).
   - **Self-published research guard** — when the RSS `<source>` (publisher) equals the account, drops research/commentary headlines (predicts/forecasts/reports) that aren't events *happening to* the company.
2. **Rule matching** against `NEWS_RULES` pattern sets.
3. Emits a `SignalCandidate` with a confidence, amount/round-stage extraction where relevant, and a `natural_key`.

### Signal types emitted

`funding_round`, `ipo_filing`, `ipo_pricing`, `ma_acquirer`, `ma_target`, `layoff`, `product_launch`, `office_open`, `award`, `certification`, `exec_hire`, `exec_departure`, `market_consolidation`, `competitor_outage`, `earnings_warning` — plus blog-derived signals via `company_feed` / `blog_to_candidates`.

## Extending

- **Add a SERP search keyword** (higher recall) — append to `serp_keywords` in `config/sources.yaml`. Phrase keywords (e.g. `product launch`) produce queries like `"CompanyName" product launch`.
- **Add a signal rule** (new signal type) — add a `NewsRule` to `NEWS_RULES` in `classify.py`.
- **If the new keyword/name collides with an English word** (e.g. `signal`, `slack`) — also add it to `_COMMON_WORD_NAMES` in `classify.py` so the common-word guard applies.

## Layout

| File | Role |
|---|---|
| `collector.py` | `NewsRssSource`, `GoogleNewsSource`, `CompanyFeedSource` — `plan()`/`parse()` |
| `feeds.py` | `google_news_search_url`, `google_news_topic_url`, `google_news_url`, `bing_news_url`, `parse_feed`, `_unwrap`, feed discovery |
| `classify.py` | `classify_news`, `NEWS_RULES`, attribution guards (`_strip_source_attribution`, common-word + self-published guards) |
| `serp_config.py` | `load_google_news_cfg()` — reads `serp_keywords` from `sources.yaml` |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_news_feeds.py tests/test_news_classify.py tests/test_news_collector_parse.py -v
```

- `test_news_feeds.py` — URL builders, feed parse/unwrap, SERP config loader.
- `test_news_classify.py` — rules, guard behavior, live-test regression cases (Gartner as publisher, Levitate festival/artwork) frozen so the false positives can't return.
- `test_news_collector_parse.py` — end-to-end `parse()` on frozen RSS fixtures.

All offline — fixtures under `tests/fixtures/news/`, no live network (conftest blocks `httpx.Client.send`).

## Ethics

- Polite RSS polling of public news feeds; respects the existing rate discipline.
- The classifier's attribution guards exist specifically to avoid mis-attributing coverage: a company that merely *publishes* research is not treated as having been the subject of an event.
- No paywall circumvention, no auth bypass.

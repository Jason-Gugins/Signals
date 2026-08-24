# Techstack

BuiltWith-style vendor ID from a company **domain**. One Signals source (`techstack`). No paid BuiltWith / Wappalyzer API.

The **index is what the wire shows**, not a global software catalog. Every distinctive third-party host becomes a `technologies` row (`host:cdn.example`). `config/fingerprints.yaml` only **names** a majority of common platforms when that host is already known.

Weekly collect fingerprints `GET https://{domain}/` HTML. When `browser.enabled` is true, a second task captures Playwright **network hosts** (HAR-lite: host + path, query stripped, no response bodies). HTML stays the default; the network task is skipped (not failed) if the browser is off.

## Run

Account must be seeded first.

```powershell
.\.venv\Scripts\python.exe -m src.cli collect --source techstack --force
```

Optional network pass — set in `config/default.yaml` (or override):

```yaml
browser:
  enabled: true
  headless: true
```

Cadence is 168h. `--force` bypasses the cursor.

## Observe first, name second

1. Collect third-party hosts from HAR-lite and/or HTML `<script src>`.
2. Drop first-party (`account.domain` / `www.`).
3. Named YAML rules **promote** matching hosts (HubSpot, Webflow, GTM, GA, Meta Pixel, CookieYes, Vector, …).
4. Leftovers stay `host:{hostname}` in `technologies` (inventory). They do **not** emit `tech_install_new`.

`harvest_tech` upserts the full promote-or-observe list. `parse` is pure and signals **named** vendors only. `tech_removed` is named-only (`host:` rows still get `missing_runs`).

## How a vendor is detected

Rules live in [`config/fingerprints.yaml`](../../../config/fingerprints.yaml).

| Evidence | Where it comes from | Example |
|---|---|---|
| `script_src` | `<script src>` in first HTML | `js.hs-scripts.com` → HubSpot |
| `network_host` | HAR-lite host list (suffix match only) | `cdn.prod.website-files.com` → Webflow |
| `dns_cname` / `mx` / `spf_include` | DNS probe (rules exist; merge is later) | `protection.outlook.com` → Microsoft 365 |
| `job_text` | text blob | `snowflake` |

`network_host` matching is suffix-only (`host == needle` or `host.endswith("." + needle)`). `force.com` does **not** match `workforce.com`. `cdn-cookieyes.com` is **not** a suffix of `cookieyes.com` — both needles are required. Needles are copied into YAML only from a frozen fixture — never guessed.

HAR-lite shape (`meta.kind=network`):

```json
{
  "page_url": "https://scanner.dev/",
  "requests": [
    {"url": "https://js.hsforms.net/forms/embed/v2.js", "host": "js.hsforms.net", "resource_type": "script"}
  ]
}
```

Cap **80 unique hosts**, third-party first (first-party CSS/fonts no longer fill the cap). Drop `data:` / `blob:`. Query strings stripped.

## Cloudflare bypass

When the HTTP fetch hits a Cloudflare challenge (403 or `challenges.cloudflare.com`), the techstack source attempts to bypass it in tiers. Detection uses `classify_cloudflare_challenge` (fingerprint.py), which inspects the response status + body and returns `"js"` (standard JS challenge), `"managed"` (Turnstile / interactive), or `None`. The bypass is scoped to `techstack` only — the runner gates on `task.source == "techstack"` so other sources retain the 403 hard-stop.

The runner stores 403 response bodies in a doc (http.py) so `classify_cloudflare_challenge` can inspect them. On a challenge, it invokes `CloudflareBypass.attempt()` and sets a `cloudflare_unsolved` meta flag. The collector gates stripping on this flag — if unsolved, only `cloudflare` is recorded (no techstack invented).

1. **Cookie reuse** — a previously-solved full Cloudflare cookie set (`cf_clearance` + `__cf_bm`), bound to the User-Agent and egress IP, is replayed via httpx. Cheapest repeat path.
2. **Browser solve** — `BrowserFetcher` creates a **fresh browser context** (no `storage_state`, so stale `session.json` cookies can't poison the solve) with the stealth init script, navigates to the URL, and polls for the page title to change from the challenge title to real content. When the title clears, it captures the full cookie jar and the real HTML.
3. **External solver** — for managed/Turnstile challenges, an optional 2Captcha/anti-captcha adapter returns a Turnstile token. The token must be re-injected into a browser context so Cloudflare's edge sets the real `cf_clearance`. **Note:** the browser-injection path is a stub (`_solver_via_browser` catches `TypeError` — `BrowserFetcher.fetch` does not yet accept `inject_turnstile_token`). The 2Captcha API call works, but the token-to-cookie conversion is not yet wired.
4. **Headed fallback** — if `cloudflare.headed_fallback` is true, a visible browser launches and runs the same auto-solve path (no manual interaction required). Bounded by `headed_solve_timeout_ms`.
5. **Hard stop** — if all tiers fail, `cloudflare` is recorded as a named observation and a `cloudflare_block_unsolved` note is logged. No techstack is invented.

On a successful solve, the **full** `promote_or_observe` pipeline runs — all named + observed vendors are recorded, not just Cloudflare. Cookies persist in the `cloudflare_cookies` SQLite table (UA + proxy bound, real expiry from Playwright cookie `expires`), reused on the next weekly collect.

Enable the solver in `.env`:
```
CLOUDFLARE_SOLVER_PROVIDER=2captcha
CLOUDFLARE_SOLVER_API_KEY=your_key
CLOUDFLARE_BYPASS_STRATEGY=browser_first
```

### CloudflareConfig reference

All fields live in `config/default.yaml` under `cloudflare:`. Env overrides: `CLOUDFLARE_SOLVER_API_KEY`, `CLOUDFLARE_SOLVER_PROVIDER`, `CLOUDFLARE_BYPASS_STRATEGY`, `CLOUDFLARE_HEADED_FALLBACK`.

| Field | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch for the bypass |
| `bypass_strategy` | `browser_first` | `browser_first` \| `solver_first` \| `browser_only` \| `disabled` |
| `solve_timeout_ms` | `20000` | Max wait for a JS challenge to auto-solve |
| `cookie_ttl_hours` | `24` | Ceiling for stored cookie expiry (real `expires` is authoritative) |
| `headed_fallback` | `false` | Launch a visible browser if headless solve fails |
| `headed_solve_timeout_ms` | `120000` | Max wait for headed fallback |
| `solver_provider` | `null` | `2captcha` \| `anticaptcha` \| `null` |
| `solver_api_key` | `null` | API key for the solver provider |
| `min_retry_delay_s` | `2.0` | Min backoff between bypass retries |
| `max_retry_delay_s` | `8.0` | Max backoff between bypass retries |
| `max_solves_per_domain_per_24h` | `1` | Anti-escalation cap — don't hammer a domain |

`harvest_tech` lists from the HTML task and the network task are **unioned** (`merge_matches`) and upserted **once** per account/pass so HTML HubSpot is not aged by a later HAR-only page.

## Vendors in YAML now

HubSpot, Salesforce, Marketo, Google Workspace, Microsoft 365, Zendesk, Intercom, Segment, Snowflake, Workday, Statuspage, Webflow, GTM, Google Analytics, Meta Pixel, CookieYes, Vector, OneTrust, Bing UET, Vimeo, Cloudflare.

Needles for Webflow / HubSpot / GTM / GA / Meta / CookieYes / Vector were frozen from `scanner.dev`. OneTrust / Bing / Vimeo from the 2026-08-23 levitate.ai and darktrace.com HARs. See [`tests/fixtures/techstack/NETWORK.md`](../../../tests/fixtures/techstack/NETWORK.md) and [`PROBE_2026-08-23.md`](../../../tests/fixtures/techstack/PROBE_2026-08-23.md).

## Signals

`parse` is pure (no DB). Candidates:

- `tech_install_new` (named vendors only)
- `tech_removed` (named vendors after two missing runs)
- `high_ticket_tech` (enterprise tier)
- `competitor_detected`

Unknown hosts persist via `harvest_tech` → `technologies` as `host:…`.

## Layout

| File | Role |
|---|---|
| `collector.py` | `TechstackSource` — plan, parse, `harvest_tech`; gates stripping on `cloudflare_unsolved` meta flag |
| `fingerprint.py` | evidence, `observed_hosts`, `dynamic_matches`, `promote_or_observe`, `classify_cloudflare_challenge` |
| `cf_bypass.py` | `CloudflareBypass` — 5-tier bypass waterfall (`attempt()`) |
| `cf_solver.py` | 2Captcha/anti-captcha adapter — returns Turnstile token (not a cookie) |
| `dns_probe.py` | MX / SPF / CNAME |
| `http_probe.py` | re-export of HTTP extract |
| `../../../src/core/browser.py` | `fetch(..., capture_network=True)` HAR-lite; `fetch(..., capture_html=True)` challenge-aware solve with fresh context |

Runner: network tasks run on the collector thread. `_fetch_one` returns `None` when `browser` is missing. Duck-typed `harvest_tech` results are merged then upserted once.

## Add a vendor

1. Recon a real homepage. Freeze hosts under `tests/fixtures/techstack/`.
2. Add `match.network_host` **and** `script_src` (HTML-only collect) in `config/fingerprints.yaml`.
3. Test: `./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py tests/test_tech_collectors_parse.py -v`

Do not invent hostnames. Do not store full HAR bodies. Do not enable `browser.enabled` by default.

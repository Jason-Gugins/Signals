# Techstack

BuiltWith-style vendor ID from a company **domain**. One Signals source (`techstack`). No paid BuiltWith / Wappalyzer API.

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

## How a vendor is detected

Rules live in [`config/fingerprints.yaml`](../../../config/fingerprints.yaml).

| Evidence | Where it comes from | Example |
|---|---|---|
| `script_src` | `<script src>` / link href in first HTML | `js.hs-scripts.com` → HubSpot |
| `network_host` | HAR-lite host list (suffix match only) | `cdn.prod.website-files.com` → Webflow |
| `dns_cname` / `mx` / `spf_include` | DNS probe (rules exist; merge is later) | `protection.outlook.com` → Microsoft 365 |
| `job_text` | text blob | `snowflake` |

`network_host` matching is suffix-only (`host == needle` or `host.endswith("." + needle)`). `force.com` does **not** match `workforce.com`. Needles are copied into YAML only from a frozen fixture — never guessed.

HAR-lite shape (`meta.kind=network`):

```json
{
  "page_url": "https://scanner.dev/",
  "requests": [
    {"url": "https://js.hsforms.net/forms/embed/v2.js", "host": "js.hsforms.net", "resource_type": "script"}
  ]
}
```

Cap 80 requests. Drop `data:` / `blob:`. Query strings stripped.

## Vendors in YAML now

HubSpot, Salesforce, Marketo, Google Workspace, Microsoft 365, Zendesk, Intercom, Segment, Snowflake, Workday, Statuspage, Webflow.

Webflow + HubSpot `network_host` needles were frozen from a live `scanner.dev` capture (2026-08-23). See [`tests/fixtures/techstack/NETWORK.md`](../../../tests/fixtures/techstack/NETWORK.md).

## Signals

`parse` is pure (no DB). Candidates:

- `tech_install_new`
- `tech_removed` (after two missing runs — via `upsert_technologies`)
- `high_ticket_tech` (enterprise tier)
- `competitor_detected`

`parse` currently emits `tech_install_new` for every match on that run (`gone=[]`). Inventory upsert is a later hook (`local_harvest`).

## Layout

| File | Role |
|---|---|
| `collector.py` | `TechstackSource` — plan HTML + network, parse both |
| `fingerprint.py` | `HttpEvidence` / `NetworkEvidence`, `match_fingerprints` |
| `dns_probe.py` | MX / SPF / CNAME |
| `http_probe.py` | re-export of HTTP extract |
| `../../../src/core/browser.py` | `fetch(..., capture_network=True)` HAR-lite |

Runner: network tasks run on the collector thread (Playwright is not thread-pool safe). `_fetch_one` returns `None` when `browser` is missing so weekly HTTP collect stays green.

## Add a vendor

1. Recon a real homepage. Freeze hosts under `tests/fixtures/techstack/`.
2. Add `match.network_host` and/or `script_src` in `config/fingerprints.yaml`.
3. Test: `./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py tests/test_tech_collectors_parse.py -v`

Do not invent hostnames. Do not store full HAR bodies. Do not enable `browser.enabled` by default.

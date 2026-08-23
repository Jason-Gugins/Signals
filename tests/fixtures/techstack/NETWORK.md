# scanner.dev homepage network recon (2026-08-23)

Live Playwright Chromium, headless. `page.goto https://scanner.dev/` `wait_until=domcontentloaded` then 2500ms.

- HTTP **200**
- Fixture: `tests/fixtures/techstack/network_scanner.dev.json`
- 79 requests after dropping `data:` / `blob:` (cap 80)
- Query strings stripped (`scheme://host/path` only)

Distinctive third-party hosts actually present:

| host | likely vendor |
|---|---|
| `cdn.prod.website-files.com` | Webflow CDN |
| `js.hsforms.net` | HubSpot forms |
| `hubspotonwebflow.com` | HubSpot + Webflow |
| `forms-na2.hsforms.com` | HubSpot forms |
| `www.googletagmanager.com` | Google Tag Manager |
| `www.google-analytics.com` | Google Analytics |
| `connect.facebook.net` | Meta Pixel |
| `cdn-cookieyes.com` | CookieYes |
| `cdn.vector.co` | Vector |

Also seen (not used as new YAML needles unless a later task needs them):
`ajax.googleapis.com`, `fonts.googleapis.com`, `fonts.gstatic.com`, `d3e54v103j8qbb.cloudfront.net`, `pagead2.googlesyndication.com`, `api.vector.co`, `directory.cookieyes.com`, `log.cookieyes.com`, `capi-automation.s3.us-east-2.amazonaws.com`.

First-party: `scanner.dev`.

Absent (do **not** add to YAML from this recon): Segment (`cdn.segment.com`), Intercom, Marketo, Salesforce Live Agent.

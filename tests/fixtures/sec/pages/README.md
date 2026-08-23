# Homepage peel fixtures (2026-08-23)

Trimmed from live `GET https://{host}/` (HTTP 200) via `scripts/recon_http.py`.
Raw bodies (gitignored): `data/recon/formd_probe_sites/{host}_root.body.html`.
Fixtures keep `<title>`, `og:site_name`, `application/ld+json`, and © / Copyright lines. CSS/JS dropped.

| Host | Status | Title | Copyright observed | ld+json `legalName` |
|---|---|---|---|---|
| scanner.dev | 200 | `Scanner \| Home` | `© 2026 Scanner, Inc.` | none |
| radicl.com | 200 | `Managed Cybersecurity & CMMC Compliance for DIB & Regulated Industries` | `© 2026 \| RADICL`; also `Copyright 2025 Fonticons, Inc.` (deny-list) | **yes** — Organization `name`=`RADICL`, `legalName`=`RADICL Defense`; addressLocality Boulder |
| parallel.ai | 200 | `Parallel - Web Infrastructure for AI Agents` | none | no `legalName`. Organization `name`=`Parallel Web Systems`, `alternateName`=`Parallel`. `og:site_name`=`Parallel`. Live ld+json `@context` is already broken (`https://***@type`) — peel must not require valid JSON-LD context |
| beamable.com | 200 | `Beamable - Game Server and LiveOps Platform for Web2/Web3` | `© 2025 Beamable` | no `legalName`. Organization/WebSite `name`=`Beamable`. `og:site_name`=`Beamable` |

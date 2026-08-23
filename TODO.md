# Techstack Cloudflare Bypass — TODO

## Live validation (DONE — clutch.co)

- [x] Run `collect --source techstack --force` against clutch.co with `browser.enabled: true`
- [x] Confirm bypass solves the challenge and records real vendors (GTM, cookiebot)
- [x] Confirm `cf_clearance` persisted to `cloudflare_cookies` table (method=browser)
- [ ] Re-run → confirm cookie-reuse tier succeeds without re-solving (blocked: stale session.json causes Cloudflare to serve a harder challenge on the second run — needs BrowserFetcher to skip storage_state on capture_html solve)

## Live validation fixes applied (commit pending)

- http.py: store 403 response bodies in a doc so the runner can classify them
- runner.py: detect 403 challenges even when result.ok=False; let bypass-failed results through to the collector with cloudflare_unsolved=True
- browser.py: _challenge_cleared polls title change (not just cookie presence) — cookie fires before page reload; added _wait_for_real_content + re-navigate fallback
- fingerprint.py: removed `challenge-platform` from managed markers (false positive on real pages with CF Turnstile widgets)

## Known issues

- Stale `data/state/session.json` with old cf_clearance causes Cloudflare to serve a harder challenge on subsequent runs. Workaround: `rm data/state/session.json` before each run. Fix: BrowserFetcher should skip storage_state when doing a capture_html challenge solve (fresh context for each solve).

## 2Captcha / Cloudflare provider setup (for managed/Turnstile challenges)

- [ ] Create a 2Captcha account at https://2captcha.com (or anti-captcha.com)
- [ ] Get an API key from the dashboard
- [ ] Add to `.env` (not committed):
  ```
  CLOUDFLARE_SOLVER_PROVIDER=2captcha
  CLOUDFLARE_SOLVER_API_KEY=<your_key>
  CLOUDFLARE_BYPASS_STRATEGY=browser_first
  ```
- [ ] Verify `TurnstileTaskProxyless` vs `AntiCloudflareTaskProxyless` task type against a real managed-challenge site
- [ ] Confirm the solver tier returns a Turnstile token and the browser re-injection path converts it to `cf_clearance`
- [ ] Test headed fallback (`CLOUDFLARE_HEADED_FALLBACK=true`) on a site that requires manual solve

## Configuration checklist

- [ ] `config/default.yaml` → `browser.enabled: true` (for live runs only; default is false)
- [ ] `config/default.yaml` → `browser.headless: true` (set false for headed fallback testing)
- [ ] `config/default.yaml` → `cloudflare.enabled: true`
- [ ] `config/default.yaml` → `cloudflare.bypass_strategy: browser_first`
- [ ] If using a proxy: set `browser.proxy_server` — httpx replay shares the same IP (IP consistency fix)
- [ ] `rm data/state/session.json` before each run until stale-session fix is applied

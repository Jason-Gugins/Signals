"""P2 Task 6 spike: source-availability probe for 5 candidate sources.

One paced reconnaissance pass per host to decide each source's fetch model
(static HTML / JSON / client-rendered JS shell / blocked) BEFORE any build.

Anti-bot discipline (user directive):
  * ONE request per probe URL; at most a second "shape" URL where the plan
    lists a mirror (e.g. old.reddit.com).
  * >=5 seconds sleep between ANY two requests, across the whole run.
  * If the FIRST response of a host looks like an anti-bot challenge
    (403/CF/DataDome/JS-challenge markers), mark NO-GO and never retry it.

Usage: python scripts/p2_source_spike.py            # all 5 hosts, sequential
       python scripts/p2_source_spike.py --host reddit   # one host
Writes: data/probe/p2_spike_<host>_<n>.<ext> raw dumps +
        data/probe/p2_source_spike_findings.json (consumed by the findings MD).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core.curl_fetcher import CurlCffiFetcher  # noqa: E402

OUT_DIR = Path("data/probe")
OUT_DIR.mkdir(parents=True, exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PACE_S = 5.0

CHALLENGE_MARKERS = (
    "just a moment", "cf-browser-verification", "attention required",
    "cf_chl_", "__cf_chl", "datadome", "dd-challenge", "captcha",
    "enable javascript and cookies", "checking your browser",
    "verify you are a human", "_pxhd", "px-captcha", "x-datadome",
)

# host key -> list of (label, url, save_ext). Max 2 entries per host.
HOST_TARGETS: dict[str, list[tuple[str, str, str]]] = {
    "reddit": [
        ("reddit-json", "https://www.reddit.com/r/sales.json?limit=25", "json"),
    ],
    "reddit_old": [
        ("old-reddit-html", "https://old.reddit.com/r/sales/", "html"),
    ],
    "yc": [
        ("yc-batch", "https://www.ycombinator.com/companies?batch=S24", "html"),
    ],
    "producthunt": [
        ("ph-product", "https://www.producthunt.com/products/notion", "html"),
    ],
    "appstores": [
        ("itunes-rss", "https://itunes.apple.com/us/rss/customerreviews/id=409183871/sortby=mostrecent/json", "json"),
        ("play-reviews", "https://play.google.com/store/apps/details?id=com.notion.app&hl=en&gl=US", "html"),
    ],
    "bbb": [
        ("bbb-profile", "https://www.bbb.org/us/ny/new-york/profile/software/notion-labs-inc-0121-87283855", "html"),
    ],
}

# host key -> (plan task it gates, human name)
HOST_META = {
    "reddit": ("T7", "Reddit r/<subreddit> public JSON (www)"),
    "reddit_old": ("T7", "Reddit old.reddit.com HTML mirror (distinct host per plan)"),
    "yc": ("T8", "Y Combinator batch directory"),
    "producthunt": ("T10", "Product Hunt product pages"),
    "appstores": ("T9", "App Store RSS + Play Store reviews"),
    "bbb": ("T11", "BBB profile pages"),
}


def looks_like_challenge(status: int, text: str) -> tuple[bool, str]:
    """(is_challenge, marker_hit). PURE."""
    if status in (403, 503, 429) and status == 403:
        pass  # body check decides
    low = text[:8000].lower()
    for marker in CHALLENGE_MARKERS:
        if marker in low:
            return True, marker
    if status == 403 and "<html" not in low and "datadome" not in low:
        return True, "403-no-html"
    return False, ""


def classify(status: int, text: str, headers: dict) -> str:
    """Response classification. PURE."""
    challenge, _ = looks_like_challenge(status, text)
    if challenge:
        return "challenge-blocked"
    if status == 404:
        return "404"
    if status != 200:
        return f"http-{status}"
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        return "server-rendered-json"
    # JS shell heuristics: tiny body, root div w/o meaningful text, heavy SPA
    if "<script" in text and "__NEXT_DATA__" not in text and "ng-version" not in text:
        if len(re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I).strip()) < 3000:
            return "client-rendered-js-shell"
        if 'id="root"' in text or 'id="__next"' in text or 'id="app"' in text:
            body_after_scripts = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I)
            m = re.search(r"<(?:div|body)[^>]*>([^<]{40,})", body_after_scripts)
            if not m:
                return "client-rendered-js-shell"
    return "server-rendered-html"


def snip(text: str, pattern: str, n: int = 3) -> list[str]:
    """First n non-overlapping matches of pattern, as raw snippets. PURE."""
    out = []
    for m in re.finditer(pattern, text, re.I):
        out.append(m.group(0)[:300])
        if len(out) >= n:
            break
    return out


SELECTOR_PROBES: dict[str, list[str]] = {
    "reddit": [r'"title"\s*:', r'"subreddit"\s*:', r'"id"\s*:\s*"t3_[a-z0-9]+"', r'"score"\s*:'],
    "reddit_old": [r'<div class="[^"]*thing[^"]*"', r'class="title[^"]*"', r'<a class="title[^>]*href="[^"]+"', r'class="score[^"]*"'],
    "yc": [r'class="[^"]*yca[^"]*"', r'href="/companies/[^"]+"', r'<tr[^>]*>', r'class="[^"]*batch[^"]*"'],
    "producthunt": [r'__NEXT_DATA__[^>]{0,80}', r'<script[^>]*id="__NEXT_DATA__"[^>]*>', r'data-test="[^"]+"', r'href="/products/[^"]+"'],
    "appstores": [r'"author"', r'"content"\s*:', r'"rating"\s*:', r'<h1[^>]*>[^<]+</h1>'],
    "bbb": [r'itemprop="[^"]+"', r'class="[^"]*rating[^"]*"', r'class="[^"]*complaint[^"]*"', r'<title>[^<]*</title>'],
}


def probe_host(fetcher: CurlCffiFetcher, host: str) -> dict:
    meta_task, meta_name = HOST_META[host]
    findings: dict = {"host": host, "name": meta_name, "gates": meta_task, "requests": []}
    for i, (label, url, ext) in enumerate(HOST_TARGETS[host]):
        if i:
            print(f"  sleeping {PACE_S}s between requests...", flush=True)
            time.sleep(PACE_S)
        save_name = f"p2_spike_{label}.{ext}"
        req: dict = {"label": label, "url": url}
        try:
            r = fetcher.get(url, headers={"Accept": "text/html,application/json,*/*"})
            text = r.body.decode("utf-8", errors="replace")
            (OUT_DIR / save_name).write_text(text, encoding="utf-8")
            cls = classify(r.status, text, dict(r.headers))
            req.update({
                "status": r.status,
                "bytes": len(text),
                "classification": cls,
                "challenge_marker": looks_like_challenge(r.status, text)[1],
                "saved": f"data/probe/{save_name}",
                "selector_hits": {p: snip(text, p) for p in SELECTOR_PROBES[host]},
            })
            print(f"  [{label}] status={r.status} bytes={len(text)} -> {cls}", flush=True)
            # Abort rule: challenge on the FIRST request of a host -> stop touching it
            if i == 0 and cls == "challenge-blocked":
                req["aborted_host"] = True
                findings["requests"].append(req)
                findings["verdict"] = "NO-GO"
                print(f"  !! challenge on first request of {host} — aborting this host", flush=True)
                return findings
        except Exception as e:  # noqa: BLE001
            req["error"] = repr(e)
            print(f"  [{label}] ERROR {e!r}", flush=True)
        findings["requests"].append(req)

    # Verdict logic over the requests actually made
    reqs = findings["requests"]
    first = reqs[0] if reqs else {}
    cls_first = first.get("classification", "error")
    if cls_first in ("server-rendered-html", "server-rendered-json"):
        findings["verdict"] = "GO plain-fetch"
    elif cls_first == "challenge-blocked":
        findings["verdict"] = "NO-GO"
    elif cls_first == "client-rendered-js-shell":
        findings["verdict"] = "GO browser-tier (or synthetic)"
    elif cls_first == "404":
        findings["verdict"] = "STUB fixture-only"
    else:
        # second shape may still be usable (e.g. reddit .json 403 -> old.reddit)
        second = reqs[1] if len(reqs) > 1 else {}
        if second.get("classification") in ("server-rendered-html", "server-rendered-json"):
            findings["verdict"] = "GO plain-fetch (via second shape)"
        elif second.get("classification") == "challenge-blocked":
            findings["verdict"] = "NO-GO"
        else:
            findings["verdict"] = "STUB fixture-only"
    return findings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", choices=sorted(HOST_TARGETS), help="probe a single host")
    args = ap.parse_args()

    hosts = [args.host] if args.host else list(HOST_TARGETS)
    fetcher = CurlCffiFetcher(user_agent=UA, impersonate="chrome")
    all_findings: dict[str, dict] = {}
    for hi, host in enumerate(hosts):
        print(f"=== {host} ({HOST_META[host][1]}) ===", flush=True)
        all_findings[host] = probe_host(fetcher, host)
        print(f"  verdict: {all_findings[host]['verdict']}", flush=True)
        # pacing between HOSTS too (>=4s required; we use PACE_S)
        if hi < len(hosts) - 1:
            print(f"  sleeping {PACE_S}s before next host...", flush=True)
            time.sleep(PACE_S)

    out_path = OUT_DIR / "p2_source_spike_findings.json"
    existing = {}
    if out_path.exists():  # merge with earlier single-host runs
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    existing.update(all_findings)
    out_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"\nfindings -> {out_path}")


if __name__ == "__main__":
    main()

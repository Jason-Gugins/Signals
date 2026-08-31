"""Spike: how does Capterra search map a company name to /p/<numeric-id>/<Slug>/?

Max 3 paced requests (5s apart) to capterra.com. Tries the HTML search page
first; if results are JS-only, probes the JSON API the page hydrates from.
Pure stdin/stdout — prints findings, saves raw HTML to data/probe/ for inspection.
"""
from __future__ import annotations

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

PRODUCT_RE = re.compile(r"/p/(\d+)/([^/\"'?\\]+)")


def extract_segments(html: str) -> list[tuple[str, str]]:
    """All /p/<id>/<Slug> segments in document order, deduped. PURE."""
    seen, out = set(), []
    for pid, slug in PRODUCT_RE.findall(html):
        if (pid, slug) not in seen:
            seen.add((pid, slug))
            out.append((pid, slug))
    return out


def main() -> None:
    fetcher = CurlCffiFetcher(user_agent=UA, impersonate="chrome")
    # (label, url, save_name) — max 3 hits
    targets = [
        ("search-html", "https://www.capterra.com/search/?query=jira", "capterra_search_jira.html"),
        ("search-next", "https://www.capterra.com/search/?query=jira&autocorrect=true", "capterra_search_jira2.html"),
        ("search-api", "https://www.capterra.com/api/search?q=jira", "capterra_search_api.json"),
    ]
    findings: dict = {}
    for i, (label, url, save_name) in enumerate(targets):
        if i:
            print(f"sleeping 5s before request {i+1}...")
            time.sleep(5)
        try:
            r = fetcher.get(url, headers={"Accept": "text/html,application/json,*/*"})
            body = r.body
            text = body.decode("utf-8", errors="replace")
            (OUT_DIR / save_name).write_text(text, encoding="utf-8")
            segs = extract_segments(text)
            findings[label] = {
                "url": url,
                "status": r.status,
                "bytes": len(text),
                "product_segments": segs[:15],
                "segment_count": len(segs),
                "is_json": text.lstrip()[:1] in "{[",
            }
            print(f"[{label}] status={r.status} bytes={len(text)} segments={len(segs)}")
            print("  sample:", segs[:8])
        except Exception as e:  # noqa: BLE001
            findings[label] = {"url": url, "error": repr(e)}
            print(f"[{label}] ERROR {e!r}")

    (OUT_DIR / "capterra_search_findings.json").write_text(
        json.dumps(findings, indent=2), encoding="utf-8")
    print("\n--- summary ---")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "product_segments"}
                      for k, v in findings.items()}, indent=2))


if __name__ == "__main__":
    main()

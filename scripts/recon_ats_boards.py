"""Probe documented ATS JSON endpoints. Never imported by collectors."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


CANDIDATES = {
    "ashby": ["GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"],
    "workable": [
        "GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
        "GET https://{token}.workable.com/spi/v3/jobs",
    ],
    "recruitee": ["GET https://{token}.recruitee.com/api/offers/"],
    "workday": ["POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"],
    "bamboohr": [
        "GET https://{token}.bamboohr.com/careers/list",
        "GET https://{token}.bamboohr.com/careers/list.json",
        "GET https://{token}.bamboohr.com/jobs/embed.json",
    ],
    "jazzhr": [
        "GET https://{token}.applytojob.com/api/jobs",
        "GET https://{token}.applytojob.com/jobs.json",
        "GET https://{token}.applytojob.com/",
    ],
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)[:60]


def urls_for(vendor: str, token: str) -> list[tuple[str, str, dict | None]]:
    vendor = vendor.casefold()
    specs = CANDIDATES.get(vendor)
    if not specs:
        return []
    out: list[tuple[str, str, dict | None]] = []
    if vendor == "workday":
        parts = token.split("/")
        tenant = parts[0]
        wd = parts[1] if len(parts) > 1 else "wd5"
        site = parts[2] if len(parts) > 2 else tenant
        from src.sources.ats.workday import workday_body, workday_endpoint

        url = workday_endpoint(tenant, wd, site)
        out.append(("POST", url, workday_body(0)))
        return out
    for spec in specs:
        method, _, tmpl = spec.partition(" ")
        out.append((method, tmpl.format(token=token), None))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("vendor", nargs="?", help="ashby|workable|recruitee|workday|bamboohr|jazzhr")
    p.add_argument("token", nargs="?", help="board token or tenant/wd/site")
    p.add_argument("--out", default="data/recon")
    args = p.parse_args(argv)
    if not args.vendor or not args.token:
        if argv is not None and "--help" not in argv:
            print("vendor and token required", file=sys.stderr)
            return 2
        p.print_help()
        return 0
    vendor = args.vendor.casefold()
    if vendor not in CANDIDATES:
        print(f"unknown vendor {args.vendor}", file=sys.stderr)
        return 2
    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    import httpx

    any_ok = False
    for method, url, body in urls_for(vendor, args.token):
        slug = f"{vendor}_{_slug(args.token)}_{_slug(urlparse(url).path or 'root')}"
        try:
            if method == "POST":
                resp = httpx.post(url, json=body, timeout=30, follow_redirects=True)
            else:
                resp = httpx.get(url, timeout=30, follow_redirects=True)
        except Exception as exc:
            (dest / f"{slug}.status.txt").write_text(f"error {exc}\n{url}\n", encoding="utf-8")
            print(f"error {exc} {url}")
            continue
        (dest / f"{slug}.status.txt").write_text(f"{resp.status_code} {url}\n", encoding="utf-8")
        (dest / f"{slug}.headers.json").write_text(json.dumps(dict(resp.headers), indent=2), encoding="utf-8")
        ctype = resp.headers.get("content-type", "")
        ext = "json" if "json" in ctype else "html" if "html" in ctype else "bin"
        (dest / f"{slug}.body.{ext}").write_bytes(resp.content)
        print(resp.status_code, len(resp.content), ctype, url)
        if resp.status_code == 200:
            any_ok = True
    return 0 if True else (0 if any_ok else 0)


if __name__ == "__main__":
    raise SystemExit(main())

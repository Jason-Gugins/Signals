"""HTTP recon: save headers + body for a URL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def slugify_url(url: str) -> str:
    host = urlparse(url).netloc or "host"
    path = (urlparse(url).path or "root").strip("/") or "root"
    raw = f"{host}_{path}"
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)[:80]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--post-json", default=None)
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--out", default="data/recon")
    args = p.parse_args(argv)
    slug = slugify_url(args.url)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {}
    for item in args.header:
        if "=" in item:
            k, v = item.split("=", 1)
            headers[k] = v
    try:
        import httpx

        if args.post_json:
            resp = httpx.post(args.url, headers=headers, json=json.loads(args.post_json), timeout=30)
        else:
            resp = httpx.get(args.url, headers=headers, timeout=30, follow_redirects=True)
        (out / f"{slug}.headers.json").write_text(json.dumps(dict(resp.headers), indent=2), encoding="utf-8")
        ctype = resp.headers.get("content-type", "")
        ext = "json" if "json" in ctype else "html" if "html" in ctype else "xml" if "xml" in ctype else "bin"
        (out / f"{slug}.body.{ext}").write_bytes(resp.content)
        print(resp.status_code, len(resp.content), ctype)
        text = resp.text[:2000]
        print("\n".join(text.splitlines()[:40]))
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

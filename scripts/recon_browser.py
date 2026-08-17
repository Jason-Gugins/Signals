"""Browser recon: save HTML, PNG, and a DOM outline."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.recon_http import slugify_url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--wait-selector", default=None)
    p.add_argument("--scroll", action="store_true")
    args = p.parse_args(argv)
    day = date(2026, 8, 16).isoformat()
    out = Path("data/recon") / day
    out.mkdir(parents=True, exist_ok=True)
    slug = slugify_url(args.url)
    print(f"would capture {args.url} -> {out / slug} headed={args.headed}")
    (out / f"{slug}.dom.md").write_text(f"# {args.url}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

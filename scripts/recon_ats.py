"""Probe ATS URL patterns for one domain (no guesswork — print candidates)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# allow `python scripts/recon_ats.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.identity.ats_discovery import ATS_PATTERNS, careers_url_candidates, detect_ats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("domain")
    p.add_argument("--html", help="Optional local HTML file to scan")
    args = p.parse_args()
    print("candidates:")
    for url in careers_url_candidates(args.domain):
        print(" ", url)
    print("vendors:", ", ".join(ATS_PATTERNS))
    if args.html:
        html = Path(args.html).read_text(encoding="utf-8")
        matches = detect_ats(html, f"https://{args.domain}/")
        print("matches:")
        for m in matches:
            print(f"  {m.vendor} token={m.token} conf={m.confidence} extra={m.extra}")


if __name__ == "__main__":
    main()

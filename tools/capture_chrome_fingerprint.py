"""Capture the real-Chrome TLS fingerprint reference from tls.peet.ws.

Run with the Signals venv, headed (Patchright / real Chromium), writes:
  tests/fixtures/antibot/chrome_fingerprint_reference.json

Usage: .venv/Scripts/python.exe tools/capture_chrome_fingerprint.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "tests" / "fixtures" / "antibot" / "chrome_fingerprint_reference.json"

KEEP = [
    "user_agent",
    "ja3_hash",
    "ja4",
    "akamai_h2",
    "akamai_fingerprint",
    "tls_extensions",
    "alpn",
    "ciphers",
    "extensions",
]


def main() -> None:
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # DataDome-class targets only clear headed; peet.ws doesn't care but keep the pattern
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Toronto",
        )
        page = ctx.new_page()
        page.goto("https://tls.peet.ws/api/all", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        body = page.evaluate("() => document.body.innerText")
        Path(ROOT / "data" / "peet_raw.json").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "peet_raw.json").write_text(body, encoding="utf-8")
        browser.close()

    data = json.loads(body)
    tls = data.get("tls", data) if isinstance(data, dict) else {}
    ref = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "https://tls.peet.ws/api/all via real Chromium (Patchright, headed)",
    }
    for key in KEEP:
        if key in data:
            ref[key] = data[key]
    for key in ("ja3_hash", "ja4", "ciphers", "extensions"):
        if key in tls and tls[key] is not None:
            ref[key] = tls[key]
    # peet.ws exposes the extension list as objects under tls.extensions;
    # normalize to the list of extension names the parity tests compare.
    if ref.get("extensions"):
        ref["tls_extensions"] = [e.get("name", "") for e in ref["extensions"]]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ref, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    print("ja4 =", ref.get("ja4"))
    print("extensions =", len(ref.get("tls_extensions") or []))


if __name__ == "__main__":
    main()

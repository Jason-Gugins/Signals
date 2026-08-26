"""DataDome challenge detection and parameter extraction.

PURE functions — no I/O, no clock reads. These detect whether an HTTP
response is a DataDome challenge page and extract the parameters needed
to submit the challenge to a solver service (2Captcha, CapSolver).
"""
from __future__ import annotations

import re
from typing import Optional


_DD_MARKER = b"captcha-delivery.com"
_DD_SCRIPT_RE = re.compile(rb"var\s+dd\s*=\s*(\{[^}]+\})", re.DOTALL)
_DD_IFRAME_RE = re.compile(rb'src="(https://geo\.captcha-delivery\.com/captcha/[^"]+)"')


def is_datadome_challenge(*, status: int, body: bytes) -> bool:
    """True if the response is a DataDome challenge page.

    DataDome returns 403 (or sometimes 200) with a body containing
    captcha-delivery.com and a dd={} JavaScript object.
    """
    if not body:
        return False
    if _DD_MARKER not in body:
        return False
    return bool(_DD_SCRIPT_RE.search(body))


def extract_datadome_params(body: bytes) -> Optional[dict]:
    """Extract the dd={} JavaScript object from a DataDome challenge page.

    Returns a dict with keys: rt, cid, hsh, t, qp, s, e, host, cookie.
    Returns None if the page is not a DataDome challenge.
    """
    m = _DD_SCRIPT_RE.search(body)
    if not m:
        return None
    raw = m.group(1).decode("utf-8", "replace")
    # Parse the JS object — it uses single quotes, not standard JSON
    # Convert single quotes to double quotes for json.loads
    import json
    raw_json = raw.replace("'", '"')
    try:
        return json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        # Fallback: regex extract individual fields
        params = {}
        for key in ("rt", "cid", "hsh", "t", "qp", "s", "e", "host", "cookie"):
            fm = re.search(rf"'{key}'\s*:\s*'([^']*)'", raw)
            if fm:
                val = fm.group(1)
                if key in ("s",):
                    val = int(val) if val.isdigit() else val
                params[key] = val
        return params if params else None


def extract_datadome_captcha_url(body: bytes) -> Optional[str]:
    """Extract the captcha URL from the DataDome iframe src attribute."""
    m = _DD_IFRAME_RE.search(body)
    if not m:
        return None
    return m.group(1).decode("utf-8", "replace")


def is_datadome_ip_banned(params: dict) -> bool:
    """True when t=bv — the IP is banned and must be changed.

    t=fe means the captcha is solvable. t=bv means the IP is directly
    banned by DataDome and no captcha solve will work from this IP.
    """
    return params.get("t") == "bv"

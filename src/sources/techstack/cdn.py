"""npm-CDN version evidence: exact package@version refs in captured text.

jsdelivr/unpkg URLs reveal the EXACT version of a vendor SDK a site ships —
presence + version in one string, zero new fetches (this mines HTML/HAR
already captured). PURE: text in, refs out; no I/O, no clock reads.
"""
from __future__ import annotations

import re

# The package segment right after the npm CDN root — scoped names keep
# "@scope/pkg" (one embedded slash) — plus the rest of the URL, so group(0)
# is the full ref (host..path) without the scheme. "@latest" and a missing
# version both normalize to version=None.
_CDN_RE = re.compile(
    r"(?:cdn\.jsdelivr\.net/npm|unpkg\.com)/"
    r"(@[^/\s\"'<>]+/[^/\s\"'<>]+|[^/\s\"'<>]+)"
    r"[^\s\"'<>]*",
    re.IGNORECASE,
)


def extract_npm_cdn_refs(text: str) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for m in _CDN_RE.finditer(text or ""):
        seg = m.group(1)
        pkg, ver = seg, None
        if seg.startswith("@"):
            rest = seg[1:]
            if "@" in rest:
                name, ver = rest.rsplit("@", 1)
                pkg = f"@{name}"
        elif "@" in seg:
            pkg, ver = seg.rsplit("@", 1)
        if not pkg or not pkg.strip(".-"):
            continue
        if not ver or ver == "latest":
            ver = None
        key = (pkg.casefold(), ver)
        if key in seen:
            continue
        seen.add(key)
        out.append({"package": pkg, "version": ver, "url": m.group(0)})
    return out

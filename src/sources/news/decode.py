"""Google News article-token decode, implemented against HttpFetcher.

Replaces the third-party googlenewsdecoder package (which fired raw,
no-timeout requests.get calls outside the fetcher — invisible to
fetch_log and the limiter, ~21.7 min per intel run under Google's
throttling). Same wire flow, repo posture: source="google_news" tasks
get limiter pacing, fetch_log rows, conditional-GET off, 30s timeout,
and the /rss/ robots_allow prefix.

Wire flow (verified against googlenewsdecoder 0.1.7, new_decoderv2.py):
1. GET https://news.google.com/rss/articles/<token>  -> c-wiz > div[jscontroller]
   carries data-n-a-sg (signature) and data-n-a-ts (timestamp).
   The /rss/articles/ form is used because robots_allow covers /rss/ only;
   /articles/ is robots-disallowed (and was only the package's first guess).
2. POST form "f.req=..." to the batchexecute endpoint -> newline-framed JSON
   whose [1] entry carries the decoded publisher URL.

Every failure returns None — resolution can never break a parse.
"""
from __future__ import annotations

import json
from urllib.parse import quote, urlparse

from src.sources.base import FetchTask

PARAMS_URL = "https://news.google.com/rss/articles/{token}"
BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_FORM_CT = "application/x-www-form-urlencoded;charset=UTF-8"


def extract_token(link: str) -> str | None:
    """Last path segment of news.google.com /(rss/)?articles/<t> or /read/<t>."""
    try:
        parsed = urlparse(link or "")
        host = (parsed.hostname or "").casefold()
        if host != "news.google.com" and not host.endswith(".news.google.com"):
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[-2] in ("articles", "read"):
            return parts[-1]
    except Exception:
        return None
    return None


def _get_html(fetcher, url: str) -> bytes | None:
    from src.core.http import FetchResult

    try:
        result = fetcher.get(FetchTask(source="google_news", url=url, domain="news.google.com"))
    except Exception:
        return None
    if not result.ok or result.status != 200 or result.doc is None:
        return None
    return result.doc.body


def _attrs_from_params_html(body: bytes) -> dict[str, str] | None:
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(body.decode("utf-8", errors="replace"))
        nodes = root.xpath("//c-wiz/div[@jscontroller]")
        if not nodes:
            return None
        return dict(nodes[0].attrib)
    except Exception:
        return None


def fetch_decoding_params(fetcher, token: str) -> tuple[str, str] | None:
    """(signature, timestamp) from the article page, or None."""
    body = _get_html(fetcher, PARAMS_URL.format(token=token))
    if body is None:
        return None
    attrs = _attrs_from_params_html(body) or {}
    signature = attrs.get("data-n-a-sg")
    timestamp = attrs.get("data-n-a-ts")
    if not signature or not timestamp:
        return None
    return signature, timestamp


def _f_req(token: str, timestamp: str, signature: str) -> str:
    payload = [
        "Fbv4je",
        f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{token}",{timestamp},"{signature}"]',
    ]
    return f"f.req={quote(json.dumps([[payload]]))}"


def _decoded_from_batchexecute(body: bytes) -> str | None:
    try:
        text = body.decode("utf-8", errors="replace")
        parsed = json.loads(text.split("\n\n")[1])[:-2]
        return json.loads(parsed[0][2])[1] or None
    except Exception:
        return None


def post_decode(fetcher, signature: str, timestamp: str, token: str) -> str | None:
    """Decoded publisher URL from batchexecute, or None."""
    task = FetchTask(
        source="google_news",
        url=BATCHEXECUTE_URL,
        domain="news.google.com",
        method="POST",
        headers={"Content-Type": _FORM_CT},
        data_body=_f_req(token, timestamp, signature),
    )
    try:
        result = fetcher.get(task)
    except Exception:
        return None
    if not result.ok or result.status != 200 or result.doc is None:
        return None
    return _decoded_from_batchexecute(result.doc.body)


def decode_token(fetcher, token: str) -> str | None:
    """Full decode: params page then batchexecute. Any failure -> None."""
    params = fetch_decoding_params(fetcher, token)
    if params is None:
        return None
    signature, timestamp = params
    return post_decode(fetcher, signature, timestamp, token)

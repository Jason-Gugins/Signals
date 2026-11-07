"""Careers harvest Phase 1 — complete DTC careers-page collection.

Renders the STORED ATS boards (brooklinen/ashby, bubbleskincare/greenhouse —
fetched 2026-10 with 200s, never rendered), then discovers + fetches careers
pages for the remaining DTC prospect domains (the careers_page fallback only
tried /careers — brumate 404'd there; veeva's cert had expired). Persists
via the existing fetch -> rawstore -> documents path (source="careers") with
fetch_log rows. DTC only: vendor domains are reference prose, excluded.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.robotparser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.rawstore import RawStore  # noqa: E402
from src.core.textutil import html_to_text  # noqa: E402
from src.lab.tierprobe import VENDOR_DOMAINS  # noqa: E402
from src.sources.ats.render import (  # noqa: E402
    careers_url_candidates,
    render_ashby_board,
    render_greenhouse_board,
)

RUN_ID_PREFIX = "careers-harvest"
CAREERS_MARKERS = re.compile(
    r"(open (roles|positions)|we'?re hiring|join (our|the) team|job description"
    r"|apply now|current openings|careers? at|available positions)", re.I)
JOB_LINK = re.compile(r"(?:/jobs?/|/careers?/|/join-us/|/open-roles?/|/positions?/)", re.I)

STORED_BOARDS = {
    "brooklinen.com": ("ashby",
                       "SELECT doc_id, path FROM documents WHERE domain='brooklinen.com' "
                       "AND url LIKE '%includeCompensation%'"),
    "bubbleskincare.com": ("greenhouse",
                           "SELECT doc_id, path FROM documents WHERE domain='bubbleskincare.com' "
                           "AND url LIKE '%content=true'"),
}


def _log_fetch(db, run_id, *, source, domain, url, status, bytes_, error=None,
               cached=False, elapsed_ms=0):
    db.execute(
        "INSERT INTO fetch_log(run_id, source, domain, url, status, elapsed_ms,"
        " bytes, cached, error, error_class) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, source, domain, url, status, elapsed_ms, bytes_,
         1 if cached else 0, error, "other" if error else None),
    )


def _stored_board_docs(db, rs: RawStore, domain: str, vendor: str, query: str,
                       cap: int, run_id: str) -> list[dict]:
    row = db.one(query + " LIMIT 1")
    if not row:
        print(f"  [{domain}] no stored {vendor} board payload — skipping render")
        return []
    path = row["path"]
    doc_id = row["doc_id"]
    if not path or not Path(path).exists():
        path = f"data/raw/{doc_id[:2]}/{doc_id}"
    raw = gzip.open(path, "rb").read()
    payload = json.loads(raw)
    if vendor == "ashby":
        docs = render_ashby_board(payload, domain=domain)
    else:
        docs = render_greenhouse_board(payload, domain=domain)
    kept = []
    for d in docs[:cap]:
        body = d["text"].encode("utf-8")
        doc = rs.put(source="careers", url=d["url"], body=body,
                     content_type="text/plain", status=200, domain=domain)
        _log_fetch(db, run_id, source="careers", domain=domain, url=d["url"],
                   status=200, bytes_=len(body), cached=True,
                   error=None)
        kept.append({"title": d["title"], "url": d["url"], "doc_id": doc.doc_id})
    print(f"  [{domain}] rendered {len(kept)} jobs from the STORED {vendor} board (cached)")
    return kept


def _doc_id_of(db, domain: str, vendor: str) -> str:
    table = {"ashby": "%includeCompensation%", "greenhouse": "%content=true"}[vendor]
    row = db.one("SELECT doc_id FROM documents WHERE domain=? AND url LIKE ? LIMIT 1",
                 (domain, table))
    return row[0] if row else "0" * 64


def _fetch(client, url: str) -> tuple[int, str]:
    r = client.get(url, follow_redirects=True)
    return r.status_code, r.text


def _discover_and_fetch(client, rs: RawStore, db, run_id: str, domain: str,
                        cands: list[str], cap: int) -> list[dict]:
    """Try the careers-hub candidates; on a careers-ish 200, discover job
    links (the careers_page adapter's regex) and fetch up to `cap` pages."""
    kept: list[dict] = []
    # sitemap-assisted discovery (one index level: Shopify sitemaps are
    # indexes pointing at sitemap_pages_*.xml) + homepage-anchor discovery
    # (the careers_page adapter's own technique: scan anchors for career-ish
    # hrefs — the fixed candidate list misses nonstandard paths)
    try:
        status, sm = _fetch(client, f"https://www.{domain}/sitemap.xml")
        if status == 200:
            locs = re.findall(r"<loc>([^<]+)</loc>", sm)
            sub = [u for u in locs if u.endswith(".xml") and u not in locs][:3]
            for s2 in sub:
                try:
                    st2, sm2 = _fetch(client, s2)
                    if st2 == 200:
                        locs += re.findall(r"<loc>([^<]+)</loc>", sm2)
                except Exception:
                    pass
            cands = cands + [u for u in locs
                             if JOB_LINK.search(u) and u not in cands][:8]
    except Exception as exc:
        _log_fetch(db, run_id, source="careers", domain=domain,
                   url=f"https://www.{domain}/sitemap.xml", status=0, bytes_=0,
                   error=f"{type(exc).__name__}: {exc}"[:200])
    try:
        status, home = _fetch(client, f"https://{domain}/")
        if status == 200:
            import src.sources.ats.careers_page as cp

            from urllib.parse import urljoin
            for href, anchor in cp._anchor_text(home):
                if not href:
                    continue
                full = urljoin(f"https://{domain}/", href)
                if (JOB_LINK.search(full)
                        or re.search(r"career|job|hiring|join.?us|open.roles?",
                                     (anchor or ""), re.I)) and full not in cands:
                    cands.append(full)
            cands = cands[:14]
    except Exception as exc:
        _log_fetch(db, run_id, source="careers", domain=domain,
                   url=f"https://{domain}/", status=0, bytes_=0,
                   error=f"{type(exc).__name__}: {exc}"[:200])
    hub_url, hub_html = None, None
    for url in cands:
        try:
            status, text = _fetch(client, url)
        except Exception as exc:
            _log_fetch(db, run_id, source="careers", domain=domain, url=url,
                       status=0, bytes_=0, error=f"{type(exc).__name__}: {exc}"[:200])
            continue
        _log_fetch(db, run_id, source="careers", domain=domain, url=url,
                   status=status, bytes_=len(text))
        if status == 200 and CAREERS_MARKERS.search(text[:4000]):
            hub_url, hub_html = url, text
            break
    if not hub_url:
        print(f"  [{domain}] no careers hub found across the candidates")
        return kept

    body = html_to_text(hub_html).encode("utf-8")
    doc = rs.put(source="careers", url=hub_url, body=body,
                 content_type="text/plain", status=200, domain=domain)
    kept.append({"title": "careers hub", "url": hub_url, "doc_id": doc.doc_id})

    # job-link discovery on the hub (reuse the careers_page adapter's regex)
    import src.sources.ats.careers_page as cp

    base = hub_url
    links = []
    for href, _anchor in cp._anchor_text(hub_html):
        if JOB_LINK.search(href or ""):
            from urllib.parse import urljoin
            links.append(urljoin(base, href))
    seen: set[str] = set()
    for link in links:
        if len(kept) > cap:
            break
        if link in seen or link == hub_url:
            continue
        seen.add(link)
        try:
            status, text = _fetch(client, link)
        except Exception as exc:
            _log_fetch(db, run_id, source="careers", domain=domain, url=link,
                       status=0, bytes_=0, error=f"{type(exc).__name__}: {exc}"[:200])
            continue
        _log_fetch(db, run_id, source="careers", domain=domain, url=link,
                   status=status, bytes_=len(text))
        if status != 200 or len(text) < 400:
            continue
        jbody = html_to_text(text).encode("utf-8")
        jdoc = rs.put(source="careers", url=link, body=jbody,
                      content_type="text/plain", status=200, domain=domain)
        kept.append({"title": "job page", "url": link, "doc_id": jdoc.doc_id})
    print(f"  [{domain}] hub {hub_url[:60]}… + {len(kept)-1} job pages")
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--dossiers", default="data/dossiers")
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--per-domain", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)

    run_id = RUN_ID_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    from src.core.db import Database

    db = Database(args.db)      # ONE writable connection: rawstore + fetch_log
    con = db.conn               # read queries ride the same connection
    rs = RawStore(db, args.raw_dir)

    # the DTC corpus domains (vendor domains excluded — reference prose),
    # derived from the DOSSIER PACKAGES (the decide-lab corpus), not the
    # whole documents table
    pkg_re = re.compile(r"^(?P<domain>.+?)-[0-9]{8}T[0-9]{6}")
    all_domains = sorted({m.group("domain")
                          for m in (pkg_re.match(p.name) for p in Path(args.dossiers).iterdir())
                          if m})
    dtc = [d for d in all_domains if d not in VENDOR_DOMAINS]
    covered = {d for (d,) in con.execute(
        "SELECT DISTINCT domain FROM documents WHERE source='careers'")}
    targets = [d for d in dtc if d not in covered]
    print(f"DTC domains: {len(dtc)} | already covered: {sorted(covered)} "
          f"| to harvest: {len(targets)}")

    kept_all: list[dict] = []
    headers = {"User-Agent": "SignalsCareersHarvest/1.0 (contact: ops@signals.local)"}
    robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # the SAME recorded override list the production fetch layer honors
    # (src/core/http.py robots_path_allowed) — one source of truth
    from src.core.http import robots_path_allowed
    from src.core.config import Config

    try:
        robots_allow = (Config().load_yaml("default") or {}).get("http", {}).get("robots_allow")
    except Exception:
        robots_allow = None

    def robots_ok(url: str) -> bool:
        if robots_path_allowed(url, robots_allow):
            return True  # operator-approved scoped override (config/default.yaml)
        host = url.split("/")[2]
        if host not in robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"https://{host}/robots.txt")
            try:
                rp.read()
                robots_cache[host] = rp
            except Exception:
                robots_cache[host] = None  # unreachable robots: default-allow, noted
        rp = robots_cache[host]
        return rp is None or rp.can_fetch("SignalsCareersHarvest", url)

    with httpx.Client(headers=headers, timeout=args.timeout) as client:
        # 1) render the STORED boards first (no network)
        for domain, (vendor, query) in STORED_BOARDS.items():
            kept_all += _stored_board_docs(db, rs, domain, vendor, query,
                                           args.per_domain, run_id)
        # 2) discover + fetch the rest (robots-checked)
        for domain in targets:
            if domain in STORED_BOARDS:
                continue
            cands = [c for c in careers_url_candidates(domain) if robots_ok(c)]
            if not cands:
                print(f"  [{domain}] all candidates disallowed by robots.txt — skipped")
                continue
            kept_all += _discover_and_fetch(client, rs, db, run_id, domain,
                                            cands, args.per_domain)

    db.conn.commit()
    print(f"stored {len(kept_all)} careers documents | run_id {run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

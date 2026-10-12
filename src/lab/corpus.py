"""Ledger corpus extractor — the decide-lab dev corpus (Task 1).

Walks ``<dossiers_dir>/<pkg>/decisions.jsonl`` (+ ``evidence.jsonl``), joins
each ``document_gate`` row to its raw-store document, and emits one flat dict
per row with package/run metadata attached (domain, market_profile_id,
generated_at, account) plus ``raw_tokens``/``latency_ms`` for cost
accounting.

The document join (post-fix-wave fact): gate ledger rows carry ``state_hash``
but NOT ``doc_id``. The join works by reconstructing the gate state per
stored document — state = {"doc_id", "source": account_name, "text":
truncate(text, 9600)} — the proven method from scripts/ab/report.py. The
account name is read from the accounts table (the name run_intel had in
scope), falling back to the capitalized domain prefix. ``text`` on joined
rows is the 9,600-char head the gate actually hashed — the deterministic
replay input, not the full body; ``byte_size`` carries the real size.

``citation_soundness`` rows may carry persisted ``claims`` (the opt-in
``lab.persist_claims`` ledger field: [{text, quote_span, doc_id}, ...]) —
they pass through untouched; the claims' doc_ids are that row's document
link. Gate rows whose hash no longer matches any stored doc are kept with
``doc_joined`` False and ``doc_id`` None (honesty over silently dropping).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.core.db import Database
from src.core.rawstore import RawStore
from src.core.textutil import truncate
from src.decide.gates import state_hash

# _DOC_STATE_TOKENS * _WINDOW_CHARS_PER_TOKEN (src/decide/gates.py): the
# head length the document gate hashed into its state.
_DOC_STATE_CHARS = 2400 * 4

# "<domain>-<YYYYMMDDTHHMMSS...Z>-<run_id>" — the timestamp anchors the split.
_PKG_NAME_RE = re.compile(r"^(?P<domain>.+?)-\d{8}T\d{6}")


def account_name(db: Database, domain: str) -> str:
    """The name run_intel had in scope for this domain (the state-hash
    ``source`` term): an existing account's DB name, else the capitalized
    domain prefix. Extracted from scripts/ab/report.py's proven method."""
    row = db.one("SELECT name FROM accounts WHERE domain = ?", (domain,))
    if row and row["name"]:
        return str(row["name"])
    return domain.split(".")[0].capitalize()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _iter_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(l) for l in lines if l.strip()]


def _package_meta(pkg: Path) -> dict:
    """Domain / market_profile_id / run metadata, manifest.json first."""
    manifest = _read_json(pkg / "manifest.json")
    dossier = _read_json(pkg / "dossier.json") if not manifest else {}
    domain = manifest.get("domain") or dossier.get("domain")
    if not domain:
        m = _PKG_NAME_RE.match(pkg.name)
        domain = m.group("domain") if m else pkg.name
    profile_id = manifest.get("market_profile_id")
    if profile_id is None and dossier:
        profile_id = (dossier.get("market_profile") or {}).get("profile_id")
    return {
        "package": pkg.name,
        "domain": domain,
        "market_profile_id": profile_id,
        "generated_at": manifest.get("generated_at") or dossier.get("generated_at"),
        "run_id": pkg.name.rsplit("-", 1)[-1],  # evidence rows have no run_id
    }


class _DocIndex:
    """Per-domain state_hash -> document map, built once, lazily."""

    def __init__(self, db: Database, raw_dir: str | Path) -> None:
        self._rs = RawStore(db, raw_dir)
        self._db = db
        self._by_domain: dict[str, dict[str, dict]] = {}
        self._accounts: dict[str, str] = {}

    def account(self, domain: str) -> str:
        if domain not in self._accounts:
            self._accounts[domain] = account_name(self._db, domain)
        return self._accounts[domain]

    def get(self, domain: str, state_hash_value: str | None) -> dict | None:
        if state_hash_value is None:
            return None
        if domain not in self._by_domain:
            self._by_domain[domain] = self._build(domain)
        return self._by_domain[domain].get(state_hash_value)

    def _build(self, domain: str) -> dict[str, dict]:
        name = self.account(domain)
        index: dict[str, dict] = {}
        for meta in self._rs.iter_docs(domain=domain):
            did = str(getattr(meta, "doc_id", "") or "")
            try:
                full = self._rs.get(did)
            except Exception:
                continue
            body = getattr(full, "body", None) if full is not None else None
            if body is None:
                continue
            text = (
                body.decode("utf-8", "replace")
                if isinstance(body, (bytes, bytearray))
                else str(body or "")
            )
            head = truncate(text, _DOC_STATE_CHARS) or ""
            digest = state_hash({"doc_id": did, "source": name, "text": head})
            if digest not in index:  # shared 9,600-char heads: first wins
                index[digest] = {
                    "doc_id": did,
                    "text": head,
                    "url": getattr(full, "url", None),
                    "source": getattr(full, "source", None),
                    "byte_size": getattr(full, "byte_size", None),
                    "fetched_at": getattr(full, "fetched_at", None),
                }
        return index


def extract(
    db: Database,
    dossiers_dir: str | Path,
    domains: list[str] | None = None,
    *,
    raw_dir: str | Path = "data/raw",
) -> list[dict]:
    """Flatten every dossier ledger row into one dev-corpus dict.

    ``domains`` filters packages by their manifest/dossier domain (None =
    all packages). One dict per decisions.jsonl row (``row_type`` =
    "decision") and per evidence.jsonl row (``row_type`` = "evidence"),
    packages in name order, rows in file order.
    """
    root = Path(dossiers_dir)
    wanted = set(domains) if domains else None
    index = _DocIndex(db, raw_dir)
    out: list[dict] = []

    packages = sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir()
            and ((p / "decisions.jsonl").exists() or (p / "evidence.jsonl").exists())
        ),
        key=lambda p: p.name,
    )
    for pkg in packages:
        meta = _package_meta(pkg)
        if wanted is not None and meta["domain"] not in wanted:
            continue
        account = index.account(meta["domain"])

        for row in _iter_jsonl(pkg / "decisions.jsonl"):
            flat: dict = dict(row)
            flat.update(meta)
            flat["row_type"] = "decision"
            flat["account"] = account
            if row.get("gate") == "document_gate":
                doc = index.get(meta["domain"], row.get("state_hash"))
                if doc is not None:
                    flat.update(doc)
                    flat["doc_joined"] = True
                else:
                    flat["doc_id"] = None
                    flat["doc_joined"] = False
            out.append(flat)

        for row in _iter_jsonl(pkg / "evidence.jsonl"):
            flat = dict(row)
            flat.update(meta)
            flat["row_type"] = "evidence"
            flat["account"] = account
            out.append(flat)

    return out

"""Content-addressed gzip document store + documents index."""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from src.core.db import Database
from src.core.models import Document
from src.core.textutil import sha256_hex


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RawStore:
    def __init__(self, db: Database, raw_dir: str | Path = "data/raw"):
        self.db = db
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, doc_id: str) -> Path:
        return self.raw_dir / doc_id[:2] / f"{doc_id}.gz"

    def put(
        self,
        *,
        source: str,
        url: str,
        body: bytes,
        content_type: str | None,
        status: int,
        domain: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        fetched_at: str | None = None,
    ) -> Document:
        doc_id = sha256_hex(body)
        rel = self._path_for(doc_id)
        rel.parent.mkdir(parents=True, exist_ok=True)
        existed = rel.exists()
        if not existed:
            with gzip.open(rel, "wb") as fh:
                fh.write(body)
        fetched_at = fetched_at or _now()
        existing = self.db.one("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
        if existing:
            self.db.execute(
                """
                UPDATE documents
                SET fetched_at = COALESCE(?, fetched_at),
                    etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified)
                WHERE doc_id = ?
                """,
                (fetched_at, etag, last_modified, doc_id),
            )
            row = self.db.one("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
            doc = Document.from_db_row(row)
            doc.body = body
            return doc
        doc = Document(
            doc_id=doc_id,
            source=source,
            url=url,
            domain=domain,
            content_type=content_type,
            status=status,
            byte_size=len(body),
            path=str(rel),
            etag=etag,
            last_modified=last_modified,
            fetched_at=fetched_at,
            body=body,
        )
        self.db.upsert("documents", doc.to_db_row(), pk="doc_id")
        return doc

    def get(self, doc_id: str) -> Optional[Document]:
        row = self.db.one("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
        if row is None:
            return None
        doc = Document.from_db_row(row)
        path = Path(doc.path) if doc.path else self._path_for(doc_id)
        if path.exists():
            with gzip.open(path, "rb") as fh:
                doc.body = fh.read()
        return doc

    def iter_docs(
        self,
        *,
        source: str | None = None,
        domain: str | None = None,
        since: str | None = None,
        unparsed_only: bool = False,
    ) -> Iterator[Document]:
        clauses = ["1=1"]
        params: list = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if since:
            clauses.append("fetched_at >= ?")
            params.append(since)
        if unparsed_only:
            clauses.append("parsed_at IS NULL")
        sql = "SELECT * FROM documents WHERE " + " AND ".join(clauses) + " ORDER BY fetched_at"
        for row in self.db.query(sql, params):
            yield Document.from_db_row(row)

    def mark_parsed(self, doc_id: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE documents SET parsed_at = ?, parse_error = ? WHERE doc_id = ?",
            (_now(), error, doc_id),
        )

    def prune(self, keep_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        if keep_days <= 0:
            rows = self.db.query("SELECT doc_id, path FROM documents")
        else:
            rows = self.db.query(
                "SELECT doc_id, path FROM documents WHERE fetched_at < ?",
                (cutoff,),
            )
        n = 0
        for row in rows:
            path = Path(row["path"]) if row["path"] else self._path_for(row["doc_id"])
            if path.exists():
                path.unlink()
            self.db.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))
            n += 1
        return n

    def stats(self) -> dict:
        docs = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS b FROM documents")
        by_source = {
            row["source"]: row["n"]
            for row in self.db.query(
                "SELECT source, COUNT(*) AS n FROM documents GROUP BY source ORDER BY source"
            )
        }
        return {"docs": docs["n"], "bytes": docs["b"], "by_source": by_source}

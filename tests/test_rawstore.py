"""Tests for the content-addressed gzip raw document store."""

from __future__ import annotations

from pathlib import Path

from src.core.db import Database
from src.core.rawstore import RawStore


def _store(tmp_path) -> RawStore:
    db = Database(tmp_path / "signals.db")
    return RawStore(db, raw_dir=tmp_path / "raw")


def test_put_get_roundtrip_binary_safe(tmp_path):
    store = _store(tmp_path)
    body = b"hello\x00world\xc3\xa9"
    doc = store.put(
        source="sec_edgar",
        url="https://example.com/a",
        body=body,
        content_type="application/octet-stream",
        status=200,
        domain="acme.com",
    )
    assert doc.body == body
    shard = Path(tmp_path / "raw" / doc.doc_id[:2] / f"{doc.doc_id}.gz")
    assert shard.exists()
    got = store.get(doc.doc_id)
    assert got is not None
    assert got.body == body
    assert got.doc_id == doc.doc_id


def test_identical_bytes_one_file_updates_fetched_at(tmp_path):
    store = _store(tmp_path)
    body = b"same"
    a = store.put(source="s", url="https://e/x", body=body, content_type="text/plain", status=200, fetched_at="2026-01-01")
    b = store.put(source="s", url="https://e/x", body=body, content_type="text/plain", status=200, fetched_at="2026-02-01", etag="W/1")
    assert a.doc_id == b.doc_id
    files = list(Path(tmp_path / "raw").rglob("*.gz"))
    assert len(files) == 1
    rows = store.db.query("SELECT doc_id, fetched_at, etag FROM documents")
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == "2026-02-01"
    assert rows[0]["etag"] == "W/1"


def test_different_bytes_same_url_second_document(tmp_path):
    store = _store(tmp_path)
    a = store.put(source="s", url="https://e/x", body=b"one", content_type="text/plain", status=200)
    b = store.put(source="s", url="https://e/x", body=b"two", content_type="text/plain", status=200)
    assert a.doc_id != b.doc_id
    assert store.db.one("SELECT COUNT(*) AS n FROM documents")["n"] == 2


def test_iter_unparsed_respects_mark_parsed(tmp_path):
    store = _store(tmp_path)
    a = store.put(source="s", url="https://e/a", body=b"a", content_type="text/plain", status=200, domain="a.com")
    b = store.put(source="s", url="https://e/b", body=b"b", content_type="text/plain", status=200, domain="b.com")
    store.mark_parsed(a.doc_id)
    ids = [d.doc_id for d in store.iter_docs(unparsed_only=True)]
    assert ids == [b.doc_id]


def test_prune_zero_removes_all(tmp_path):
    store = _store(tmp_path)
    store.put(source="s", url="https://e/a", body=b"a", content_type="text/plain", status=200)
    store.put(source="s", url="https://e/b", body=b"b", content_type="text/plain", status=200)
    n = store.prune(0)
    assert n == 2
    assert store.db.one("SELECT COUNT(*) AS n FROM documents")["n"] == 0
    assert list(Path(tmp_path / "raw").rglob("*.gz")) == []


def test_stats(tmp_path):
    store = _store(tmp_path)
    store.put(source="sec", url="https://e/a", body=b"aaa", content_type="text/plain", status=200)
    store.put(source="news", url="https://e/b", body=b"bb", content_type="text/plain", status=200)
    stats = store.stats()
    assert stats["docs"] == 2
    assert stats["bytes"] == 5
    assert stats["by_source"]["sec"] == 1
    assert stats["by_source"]["news"] == 1

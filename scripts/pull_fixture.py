"""Copy a stored raw document into tests/fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.db import Database
from src.core.rawstore import RawStore


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--doc", required=True, help="sha or url substring")
    p.add_argument("--dest", required=True)
    p.add_argument("--db", default="data/signals.db")
    p.add_argument("--raw", default="data/raw")
    args = p.parse_args(argv)
    db = Database(args.db)
    store = RawStore(db, args.raw)
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    row = db.one("SELECT * FROM documents WHERE doc_id = ? OR url LIKE ?", (args.doc, f"%{args.doc}%"))
    if not row:
        print("not found")
        return 1
    doc = store.get(row["doc_id"])
    body = doc.body or b""
    if (row.get("content_type") or "").find("json") >= 0:
        try:
            dest.write_text(json.dumps(json.loads(body), indent=2), encoding="utf-8")
            return 0
        except Exception:
            pass
    dest.write_bytes(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

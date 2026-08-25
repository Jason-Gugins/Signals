"""Export G2 reviews to JSON (per company) and CSV (dict per row)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from src.core.db import Database


def _review_row_to_dict(row: dict) -> dict:
    """Convert a DB row to a clean dict for export. JSON-decode pros/cons."""
    out = dict(row)
    for key in ("pros", "cons"):
        val = out.get(key)
        if val and isinstance(val, str):
            try:
                out[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    out["verified_reviewer"] = bool(out.get("verified_reviewer", 0))
    return out


def export_g2_json(db: Database, product_slug: str, export_dir: str) -> str:
    """Export all reviews for a product slug as a JSON array. One file per company."""
    rows = db.query(
        "SELECT * FROM g2_reviews WHERE product_slug=? ORDER BY posted_at DESC",
        (product_slug,),
    )
    data = [_review_row_to_dict(r) for r in rows]
    out_path = Path(export_dir) / "g2" / f"{product_slug}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(out_path)


def export_g2_csv(db: Database, product_slug: str, export_dir: str) -> str:
    """Export all reviews for a product slug as CSV (one dict per row)."""
    rows = db.query(
        "SELECT * FROM g2_reviews WHERE product_slug=? ORDER BY posted_at DESC",
        (product_slug,),
    )
    out_path = Path(export_dir) / "g2" / f"{product_slug}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return str(out_path)
    flat_rows = []
    for r in rows:
        r = dict(r)
        for key in ("pros", "cons"):
            val = r.get(key)
            if val and isinstance(val, str):
                try:
                    r[key] = "; ".join(json.loads(val))
                except (json.JSONDecodeError, TypeError):
                    pass
        r["verified_reviewer"] = bool(r.get("verified_reviewer", 0))
        flat_rows.append(r)
    fieldnames = list(flat_rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_rows)
    return str(out_path)


def export_g2_all(db: Database, export_dir: str) -> list[str]:
    """Export JSON + CSV for every product slug that has reviews."""
    slugs = [r["product_slug"] for r in db.query(
        "SELECT DISTINCT product_slug FROM g2_reviews ORDER BY product_slug"
    )]
    paths = []
    for slug in slugs:
        paths.append(export_g2_json(db, slug, export_dir))
        paths.append(export_g2_csv(db, slug, export_dir))
    return paths

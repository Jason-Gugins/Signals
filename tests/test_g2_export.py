import json
import csv
from pathlib import Path
from src.core.db import Database
from src.export.g2_export import export_g2_json, export_g2_csv

def _seed(db):
    db.upsert("g2_reviews", {
        "review_id": "abc", "product_slug": "slack", "reviewer_name": "John",
        "rating": 4.5, "review_title": "Great", "review_body": "Love it",
        "pros": '["msg"]', "cons": '["noise"]', "posted_at": "2026-01-15",
        "review_url": "https://g2.com/x", "verified_reviewer": 1,
        "review_source": "Organic", "first_seen_at": "2026-08-25",
        "last_seen_at": "2026-08-25",
    }, pk=("review_id",))

def test_export_g2_json(tmp_path):
    db = Database(tmp_path / "s.db")
    _seed(db)
    path = export_g2_json(db, "slack", str(tmp_path / "exports"))
    data = json.loads(Path(path).read_text())
    assert len(data) == 1
    assert data[0]["reviewer_name"] == "John"
    assert data[0]["rating"] == 4.5
    assert data[0]["pros"] == ["msg"]

def test_export_g2_csv(tmp_path):
    db = Database(tmp_path / "s.db")
    _seed(db)
    path = export_g2_csv(db, "slack", str(tmp_path / "exports"))
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["reviewer_name"] == "John"
    assert float(rows[0]["rating"]) == 4.5

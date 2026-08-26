"""G2 end-to-end: plan -> parse -> harvest -> export. All offline with fixture."""
import json
from pathlib import Path
from datetime import datetime, timezone

from src.core.db import Database
from src.core.models import Account, Document
from src.sources.marketplace.collector import MarketplaceG2Source, upsert_g2_reviews
from src.sources.marketplace.g2 import parse_g2_reviews
from src.export.g2_export import export_g2_json, export_g2_csv

FIXTURE = (Path("tests/fixtures/marketplace/g2_reviews.html")).read_text(encoding="utf-8")


def test_g2_e2e_offline(tmp_path):
    """Full pipeline: plan -> parse -> harvest -> export, all offline."""
    db = Database(tmp_path / "s.db")
    adapter = MarketplaceG2Source()
    acct = Account(domain="acme.com", name="Acme", g2_slug="slack")

    # 1. Plan — returns a fetch task for the G2 reviews page
    tasks = adapter.plan(acct, None)
    assert len(tasks) == 1
    assert "slack" in tasks[0].url
    assert tasks[0].source == "marketplace_g2"

    # 2. Simulate fetch — use fixture as the "fetched" body
    doc = Document(
        doc_id="d1",
        source="marketplace_g2",
        url=tasks[0].url,
        body=FIXTURE.encode("utf-8"),
    )

    # 3. Parse — returns signal candidates (only reviews within 90 days of "today")
    # Use today=2026-03-15 so only the Jan 15 review (59 days) is within 90 days;
    # Dec 3 (102d) and Nov 20 (115d) fall outside the window.
    meta = {"today": "2026-03-15"}
    cands = adapter.parse(doc, acct, meta)
    assert len(cands) == 1
    assert cands[0].signal_type == "intent_2nd_marketplace"
    assert cands[0].confidence > 0

    # 4. Harvest — persist all 3 reviews to DB (harvest_reviews returns all, not filtered by date)
    revs = adapter.harvest_reviews(doc, acct, meta)
    assert len(revs) == 3
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new_count, updated_count = upsert_g2_reviews(db, revs, now=now, raw_ref="d1")
    assert new_count == 3
    assert updated_count == 0

    # 5. Export — JSON and CSV files created from DB
    json_path = export_g2_json(db, "slack", str(tmp_path / "exports"))
    csv_path = export_g2_csv(db, "slack", str(tmp_path / "exports"))
    assert Path(json_path).exists()
    assert Path(csv_path).exists()

    # 6. Verify JSON content
    data = json.loads(Path(json_path).read_text())
    assert len(data) == 3
    assert data[0]["rating"] == 4.5  # highest rating first (sorted by posted_at DESC... actually by posted_at DESC, so Jan 15 first)
    assert data[0]["reviewer_name"] == "John D"
    assert data[0]["pros"] == ["Real-time messaging", "Integrations with other tools"]

    # 7. Verify CSV content
    import csv
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 3

    # 8. Upsert idempotency — second call updates, doesn't duplicate
    new2, updated2 = upsert_g2_reviews(db, revs, now=now, raw_ref="d1")
    assert new2 == 0
    assert updated2 == 3
    rows_after = db.query("SELECT * FROM g2_reviews WHERE product_slug=?", ("slack",))
    assert len(rows_after) == 3

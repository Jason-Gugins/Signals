from datetime import date
from pathlib import Path

from src.core.db import Database
from src.core.models import Account
from src.sources.owned.collector import OwnedIntentSource
from src.sources.repvue_db.collector import RepvueDbSource


def test_owned_local_harvest_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "visits.csv").write_text(
        Path("tests/fixtures/owned/visits.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    src = OwnedIntentSource()
    cands = src.local_harvest(
        db=Database(tmp_path / "s.db"),
        account=Account(domain="acme.com"),
        today=date(2026, 8, 16),
        task_meta={"inbox": str(inbox)},
    )
    assert any(c.signal_type == "intent_1st_owned" for c in cands)


def test_repvue_source_registered():
    assert RepvueDbSource.key == "repvue_db"
    assert RepvueDbSource.tier == "local"

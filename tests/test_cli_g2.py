import os

from click.testing import CliRunner
from src.cli import main


def test_cli_g2_export_exists(tmp_path):
    """The g2-export command exists and runs."""
    os.environ["SIGNALS_DB_PATH"] = str(tmp_path / "s.db")
    runner = CliRunner()
    result = runner.invoke(main, ["g2-export", "--slug", "nonexistent", "--dir", str(tmp_path)])
    assert result.exit_code == 0


def test_cli_g2_export_creates_files(tmp_path):
    """g2-export creates JSON and CSV files for a product slug."""
    os.environ["SIGNALS_DB_PATH"] = str(tmp_path / "s.db")
    from src.core.db import Database
    db = Database(tmp_path / "s.db")
    db.upsert("g2_reviews", {
        "review_id": "abc", "product_slug": "slack", "reviewer_name": "John",
        "rating": 4.5, "review_title": "Great", "review_body": "Love it",
        "pros": '["msg"]', "cons": '["noise"]', "posted_at": "2026-01-15",
        "review_url": "https://g2.com/x", "verified_reviewer": 1,
        "review_source": "Organic", "first_seen_at": "2026-08-25",
        "last_seen_at": "2026-08-25",
    }, pk=("review_id",))

    runner = CliRunner()
    export_dir = str(tmp_path / "exports")
    result = runner.invoke(main, ["g2-export", "--slug", "slack", "--dir", export_dir])
    assert result.exit_code == 0
    # Should have created JSON and CSV files
    g2_dir = os.path.join(export_dir, "g2")
    assert os.path.exists(os.path.join(g2_dir, "slack.json"))
    assert os.path.exists(os.path.join(g2_dir, "slack.csv"))

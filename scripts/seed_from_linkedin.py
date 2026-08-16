"""Bootstrap accounts from the local LinkedIn SQLite database."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.core.config import Config
from src.core.db import Database
from src.identity.registry import AccountRegistry
from src.identity.seeds import seed_from_linkedin_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Path to linkedin.db (default: config external_dbs.linkedin_db)")
    parser.add_argument("--cohort", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    cfg = Config.load()
    li = args.db or cfg.external_dbs.linkedin_db
    db = Database(cfg.storage.db_path)
    stats = seed_from_linkedin_db(AccountRegistry(db), li, cohort=args.cohort, limit=args.limit)
    print(f"created={stats.created} updated={stats.updated} skipped={stats.skipped} reasons={stats.reasons}")


if __name__ == "__main__":
    main()

"""Live smoke: resolve → collect → score → brief for three domains.

Not part of pytest. Touches the network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Config
from src.pipeline.orchestrator import Orchestrator


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("domains", nargs="*", default=["shopify.com", "databricks.com", "hubspot.com"])
    args = p.parse_args(argv)
    cfg = Config.load()
    orch = Orchestrator(cfg)
    for d in args.domains:
        print("==", d)
        orch.resolve(limit=1)
        stats = orch.collect(domains=[d], force=False, dry_run=True)
        print(" planned", stats.tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

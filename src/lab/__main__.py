"""``python -m src.lab`` — the decide-lab runner entry point (skeleton).

Dev-only: this package is never imported by the intel pipeline
(tests/test_lab_isolation.py enforces the import graph).

Planned subcommands (Task 9 wires the logic; nothing runs yet):
  run     execute autoresearch rounds (propose -> prescreen -> replay ->
          encode -> regress -> feed errors back), bounded by --rounds and
          --budget-tokens/--budget-$ ceilings
  report  write the calibration report + the PROPOSED config/decide.yaml diff
  corpus  extract the ledger corpus from data/dossiers (src/lab/corpus.py)
"""

from __future__ import annotations

import argparse
import sys

_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("run", "execute autoresearch rounds (propose -> prescreen -> replay -> regress)"),
    ("report", "write the calibration report + proposed decide.yaml diff"),
    ("corpus", "extract the ledger corpus from data/dossiers into flat rows"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.lab",
        description=(
            "Decide lab: offline, budget-bounded A/B + autoresearch loop that "
            "tunes the decide layer's spend stages from the decision ledger. "
            "Dev-only; never imported by the intel pipeline."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="{run,report,corpus}")
    for name, text in _SUBCOMMANDS:
        sub.add_parser(name, help=text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    print(
        f"src.lab: '{args.command}' is planned but not implemented yet — "
        "Task 9 wires run/report (see docs/plans/2026-10-11-autoresearch-decide-lab.md)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

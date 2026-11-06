"""Thin shell over src.lab.tierprobe.probe_main — the implementer tier
probe's CLI (see that module's docstring for the experiment design)."""

import sys

from src.lab.tierprobe import probe_main

if __name__ == "__main__":
    sys.exit(probe_main())

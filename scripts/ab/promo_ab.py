"""Thin shell over src.lab.promo_ab.probe_main — the label-refereed
promotion-question A/B CLI (see that module's docstring)."""

import sys

from src.lab.promo_ab import probe_main

if __name__ == "__main__":
    sys.exit(probe_main())

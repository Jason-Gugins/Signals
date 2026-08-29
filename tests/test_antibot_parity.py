"""Live fingerprint parity: engine ClientHello vs real Chromium.

These tests hit https://tls.peet.ws over the network and are SKIPPED in normal
runs. Run them on demand with:

    .venv/Scripts/python.exe -m pytest tests/test_antibot_parity.py -v -m antibot_live

The reference fixture is captured from real Chromium (Patchright, headed) via
tools/capture_chrome_fingerprint.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REF_PATH = Path(__file__).parent / "fixtures" / "antibot" / "chrome_fingerprint_reference.json"
pytestmark = [pytest.mark.antibot_live, pytest.mark.allow_network]


def _reference() -> dict:
    return json.loads(REF_PATH.read_text(encoding="utf-8"))


def _probe() -> dict:
    import signals_antibot

    return json.loads(signals_antibot.probe_fingerprint("https://tls.peet.ws/api/all"))


def _ext_names(entry) -> str:
    """Normalize an extension entry (string or dict) to its name, with random
    GREASE values masked — real Chrome sends a different GREASE value in every
    handshake, so exact hex comparison is wrong by construction."""
    if isinstance(entry, dict):
        name = str(entry.get("name", entry))
    else:
        name = str(entry)
    import re

    return re.sub(r"GREASE \(0x[0-9a-fA-F]+\)", "GREASE", name)


def _ext_names_sorted(entries) -> list[str]:
    return sorted(_ext_names(e) for e in entries)


@pytest.mark.antibot_live
def test_engine_ja4_prefix_matches_chrome():
    """JA4's stable prefix (tls version + cipher hash + ext count + alpn) must
    equal real Chrome's. The 12-hex suffix hashes the full extension list,
    which includes per-connection random GREASE values — real Chrome also
    differs there between handshakes, so compare the prefix only."""
    ref = _reference()
    fp = _probe()
    assert fp["ja4"] and ref["ja4"], f"probe: {fp.get('ja4')} ref: {ref.get('ja4')}"

    def ja4_parts(ja4: str):
        """Split JA4 't13d1516h2_<ciphhash>_<exthash>' into
        (ciph_hash, ext_count:int, alpn). The cipher hash and ALPN are stable;
        the extension count flips 16<->17 with the optional 'padding' ext."""
        first, ciph_hash, _ext_hash = ja4.split("_")
        # first = 't13d1516h2' -> strip 't13d' prefix and ALPN suffix
        core = first[len("t13d"):]          # '1516h2'
        count = core[:-2]                   # '1516'
        alpn = core[-2:]                    # 'h2'
        return ciph_hash, int(count), alpn

    ref_ciph, ref_count, ref_alpn = ja4_parts(ref["ja4"])
    fp_ciph, fp_count, fp_alpn = ja4_parts(fp["ja4"])
    assert fp_ciph == ref_ciph, f"cipher hash {fp_ciph} != chrome {ref_ciph}"
    assert fp_alpn == ref_alpn, f"alpn {fp_alpn} != chrome {ref_alpn}"
    assert abs(fp_count - ref_count) <= 1, (
        f"extension count {fp_count} vs chrome {ref_count} (diff > 1)\n"
        "(count varies by 1 with the optional 'padding' extension, as in real Chrome)"
    )


@pytest.mark.antibot_live
def test_engine_extension_set_matches_chrome():
    ref = _reference()
    fp = _probe()
    fp_names = _ext_names_sorted(fp["tls_extensions"])
    ref_names = _ext_names_sorted(ref["tls_extensions"])
    # 'padding (21)' is legitimately variable: Chrome (and BoringSSL with
    # Chrome behaviors) adds ClientHello padding only when the handshake
    # crosses a block-size threshold, which depends on random material. Real
    # Chrome flips between with/without padding between handshakes too.
    OPTIONAL = {"padding (21)"}
    fp_cmp = [n for n in fp_names if n not in OPTIONAL]
    ref_cmp = [n for n in ref_names if n not in OPTIONAL]
    assert fp_cmp == ref_cmp, (
        f"engine: {fp_names}\nchrome: {ref_names}"
    )


@pytest.mark.antibot_live
def test_engine_akamai_h2_shape_matches_chrome():
    """Akamai h2 fingerprint: SETTINGS values must match Chrome. The window-
    update component varies with timing, so compare the SETTINGS segment."""
    ref = _reference()
    fp = _probe()
    ref_fp = ref.get("akamai_h2") or ""
    fp_fp = fp.get("akamai_h2") or ""
    ref_settings = ref_fp.split("|")[0]
    fp_settings = fp_fp.split("|")[0]
    assert fp_settings == ref_settings, (
        f"engine h2 settings {fp_settings!r} != chrome {ref_settings!r}\n"
        f"full: engine {fp_fp!r} chrome {ref_fp!r}"
    )

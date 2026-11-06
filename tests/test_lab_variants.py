"""src/lab/variants.py registry-filter tests — the dedicated-run seam.

``variants_for_block`` powers ``--block`` (e.g. an output_screen-only
calibration run): one block's variants plus the zero-call incumbent anchors,
nothing else — so the pot divides across the axis under test instead of the
full 23-variant registry.
"""

import pytest

from src.lab import variants as variants_mod


def test_variants_for_block_keeps_block_variants_plus_zero_call_anchors():
    vs = variants_mod.variants_for_block("output_screen")
    ids = {v["id"] for v in vs}
    # the screen ladder, complete — INCLUDING the shipped-threshold anchor
    # (ask=True, empty delta: the same-prompt-shape comparator that isolates
    # threshold effects from prompt interaction / fresh-call variance)
    assert {
        "g4_screen_review_loose",
        "g4_screen_review_tight",
        "g4_screen_action_loose",
        "screen_shipped",
    } <= ids
    # the A-side anchors ride along (zero-call comparators, cost nothing)
    assert {"doc_incumbent", "g4_incumbent", "g5_incumbent"} <= ids
    # nothing else: no doc-gate ladder, no g4 floor/band rungs, no impl
    assert "doc_relevant_min_loose" not in ids
    assert "g4_floor_tight" not in ids
    assert "impl_prompt_claim_cap_6" not in ids
    assert len(vs) == 7


def test_screen_shipped_variant_is_the_ask_true_empty_delta():
    (v,) = [x for x in variants_mod.default_variants() if x["id"] == "screen_shipped"]
    assert v["block"] == "output_screen"
    assert v["config_delta"] == {}          # shipped thresholds, nothing moved
    assert v["requires_replay"] == "jev"    # re-asks: fresh nouls, same prompt shape
    assert v["expected_direction"] is None  # an anchor, not a rung


def test_variants_for_block_hands_out_fresh_copies():
    first = variants_mod.variants_for_block("output_screen")
    first[0]["config_delta"]["pinned"] = 1
    second = variants_mod.variants_for_block("output_screen")
    assert all("pinned" not in v["config_delta"] for v in second)


def test_variants_for_block_unknown_block_raises_loud():
    # a typo must not silently produce an empty (anchor-only) run
    with pytest.raises(ValueError, match="unknown block"):
        variants_mod.variants_for_block("output_scren")

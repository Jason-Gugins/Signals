"""Stage-variant registry (decide-lab Task 5) — the declarative A/B catalog.

One variant = ONE candidate change to ONE spend stage of the decide layer,
expressed exactly the way ``config/decide.yaml`` would carry it: a
``config_delta`` dict applied (shallowly) over the shipped gate block for
that stage. The registry is DATA, not code — :func:`default_variants` is the
round's starting ladder, and later rounds (Tasks 7-9) can register more.

Stages (one variant axis per stage, per the plan):

- ``doc_gate`` — the document gate: threshold ladders on
  ``relevant_min``/``evidence_min`` (bracketing the shipped
  :data:`src.decide.gates.DOC_RELEVANT_MIN`/:data:`DOC_EVIDENCE_MIN`) plus
  the 4th-question variant adding a ``has_profile_vocab_evidence`` Noul
  (does the doc speak the account's market-profile vocabulary?) with its own
  routing floor.
- ``g4`` — the citation gate: ``floor`` ladders bracketing
  :data:`src.decide.gates.CLAIM_FLOOR`, ``band_low`` alternates around the
  shipped 0.30, and ``output_screen`` threshold ladders (the G4 rider's
  ``review_threshold``/``action_threshold``).
- ``implementer`` — bulk-implementer PROMPT variants (claim-count caps,
  stricter quote-span requirements, ``route_threshold`` moves). These are
  OUT of replay scope in v1: replaying them means NEW deepseek calls to
  regenerate claims (the expensive axis, ~33c/domain observed at arm A), so
  they are METADATA-ONLY — ``requires_replay: "deepseek"`` and
  :func:`src.lab.replay.replay_variant` refuses them honestly
  (``status: "not_replayable"``, zero decider calls).

``requires_replay`` semantics (the replay's dispatch key):

- ``"jev"`` — replayable by re-asking the stage's Jev questions on cached
  corpus states (input-only pennies; the A/B core).
- ``None`` — replayable with ZERO calls: the variant only moves routing
  thresholds, so the RECORDED answers re-route deterministically under the
  merged config. The incumbent anchors (``doc_incumbent``/``g4_incumbent``,
  empty deltas) use this to score the shipped behavior under the same metric
  machinery — the A-side of every comparison.
- ``"deepseek"`` — needs new implementer calls; metadata-only in v1.

The incumbent anchors exist because :func:`src.lab.replay.advance` needs an
incumbent score and the honest way to get one is the same scorer over the
same rows, not a hand-written baseline number.

Expected directions are the honest prior for the report (``"up"`` = the
weighted score is expected to rise, ``"down"`` = to fall, ``"probe"`` =
genuinely uncertain), never an outcome: the replay's weighted score decides.

Stdlib only, no pipeline imports (the scope guard: src/lab never enters the
intel import graph).
"""

from __future__ import annotations

import copy

# The three spend stages (one variant axis each, per the plan).
STAGE_DOC_GATE = "doc_gate"
STAGE_G4 = "g4"
STAGE_IMPLEMENTER = "implementer"

# Replay engines a variant can require.
REPLAY_JEV = "jev"
REPLAY_DEEPSEEK = "deepseek"

# Expected-direction vocabulary (a compact prior, never an outcome).
DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_PROBE = "probe"

#: Every registry entry carries these keys (plus ``block`` naming the
#: decide.yaml gate block the delta merges over).
REQUIRED_KEYS = (
    "id",
    "stage",
    "description",
    "config_delta",
    "requires_replay",
    "expected_direction",
)

#: The 4th-question variant's added Noul: does the document use the
#: account's market-profile vocabulary? Its routing floor rides the same
#: gate block as ``<question id>_min`` (see replay.py's routing ladder).
PROFILE_VOCAB_QUESTION_ID = "has_profile_vocab_evidence"
PROFILE_VOCAB_MIN = 0.50

# --- the registry (module-private: default_variants() hands out copies) -------

_REGISTRY: tuple[dict, ...] = (
    # ------------------------------------------------ doc gate: incumbents +
    # threshold ladders on relevant_min/evidence_min + the 4th-question variant
    {
        "id": "doc_incumbent",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "The shipped document gate under the same scorer (empty delta): "
            "re-routes the recorded answers under the shipped thresholds — "
            "the zero-call A-side anchor every doc-gate variant is judged against."
        ),
        "config_delta": {},
        "requires_replay": None,
        "expected_direction": None,
    },
    {
        "id": "doc_relevant_min_loose",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "Relevance floor 0.45 -> 0.35: more marginal docs reach the "
            "implementer (recall up, precision at risk)."
        ),
        "config_delta": {"relevant_min": 0.35},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "doc_relevant_min_tight",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "Relevance floor 0.45 -> 0.55: fewer marginal docs (precision up, "
            "recall at risk)."
        ),
        "config_delta": {"relevant_min": 0.55},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "doc_evidence_min_loose",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "Evidence floor 0.55 -> 0.45: docs with thinner signal evidence "
            "are included."
        ),
        "config_delta": {"evidence_min": 0.45},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "doc_evidence_min_tight",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "Evidence floor 0.55 -> 0.65: only strong-evidence docs are "
            "included."
        ),
        "config_delta": {"evidence_min": 0.65},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_UP,
    },
    {
        "id": "doc_gate_profile_vocab",
        "stage": STAGE_DOC_GATE,
        "block": "document_gate",
        "description": (
            "4th Noul — has_profile_vocab_evidence: does the document use the "
            "account's market-profile vocabulary (served problems, qualifying "
            "signals, named competitors)? The include rule additionally "
            "requires the noul to clear has_profile_vocab_evidence_min (0.50)."
        ),
        "config_delta": {
            "questions": {
                PROFILE_VOCAB_QUESTION_ID: {
                    "type": "noul",
                    "instructions": (
                        "Does this document use vocabulary from the account's "
                        "market profile — the served problems, qualifying "
                        "signals, or named competitors? Answer noul."
                    ),
                }
            },
            f"{PROFILE_VOCAB_QUESTION_ID}_min": PROFILE_VOCAB_MIN,
        },
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_UP,
    },
    # ------------------------------------------------ g4: incumbent + floor/
    # band ladders + output_screen threshold ladders
    {
        "id": "g4_incumbent",
        "stage": STAGE_G4,
        "block": "citation_soundness",
        "description": (
            "The shipped citation gate under the same scorer (empty delta): "
            "re-routes the recorded Choice answers under the shipped floor — "
            "the zero-call A-side anchor for G4 variants."
        ),
        "config_delta": {},
        "requires_replay": None,
        "expected_direction": None,
    },
    {
        "id": "g4_floor_loose",
        "stage": STAGE_G4,
        "block": "citation_soundness",
        "description": (
            "Choice floor 0.70 -> 0.60: more claims route on their top label "
            "instead of abstaining into review."
        ),
        "config_delta": {"floor": 0.60},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "g4_floor_tight",
        "stage": STAGE_G4,
        "block": "citation_soundness",
        "description": (
            "Choice floor 0.70 -> 0.80: sub-floor probabilities abstain into "
            "review (the top-prob-or-abstain rule tightens)."
        ),
        "config_delta": {"floor": 0.80},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_UP,
    },
    {
        "id": "g4_band_low_narrow",
        "stage": STAGE_G4,
        "block": "citation_soundness",
        "description": (
            "Band low edge 0.30 -> 0.20: the recorded review band narrows "
            "(declarative ladder position; the Choice rule routes on floor, "
            "band_low is the block's informational knob)."
        ),
        "config_delta": {"band_low": 0.20},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "g4_band_low_wide",
        "stage": STAGE_G4,
        "block": "citation_soundness",
        "description": (
            "Band low edge 0.30 -> 0.40: the recorded review band widens."
        ),
        "config_delta": {"band_low": 0.40},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_PROBE,
    },
    {
        "id": "g4_screen_review_loose",
        "stage": STAGE_G4,
        "block": "output_screen",
        "description": (
            "Output-screen review threshold 0.35 -> 0.25: weaker batch "
            "hazards (out-of-excerpt facts / wrong entity) hold batches for "
            "review more often."
        ),
        "config_delta": {"review_threshold": 0.25},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_UP,
    },
    {
        "id": "g4_screen_review_tight",
        "stage": STAGE_G4,
        "block": "output_screen",
        "description": (
            "Output-screen review threshold 0.35 -> 0.45: only stronger "
            "batch hazards hold batches for review."
        ),
        "config_delta": {"review_threshold": 0.45},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_DOWN,
    },
    {
        "id": "g4_screen_action_loose",
        "stage": STAGE_G4,
        "block": "output_screen",
        "description": (
            "Output-screen action threshold 0.70 -> 0.60: batch hazards block "
            "(drop all claims) sooner."
        ),
        "config_delta": {"action_threshold": 0.60},
        "requires_replay": REPLAY_JEV,
        "expected_direction": DIRECTION_DOWN,
    },
    # ------------------------------------------------ implementer: prompt
    # variants, OUT of replay scope — metadata-only (deepseek)
    {
        "id": "impl_prompt_claim_cap_6",
        "stage": STAGE_IMPLEMENTER,
        "block": "implementers.bulk",
        "description": (
            "Bulk-implementer prompt variant: cap extracted claims at 6 per "
            "document (fewer, higher-confidence claims). NOT replayable in "
            "v1 — regenerating claims means new deepseek calls (Task 2's "
            "backfill cache is the future replay surface)."
        ),
        "config_delta": {"prompt": {"max_claims": 6}},
        "requires_replay": REPLAY_DEEPSEEK,
        "expected_direction": DIRECTION_UP,
    },
    {
        "id": "impl_prompt_strict_quote_span",
        "stage": STAGE_IMPLEMENTER,
        "block": "implementers.bulk",
        "description": (
            "Bulk-implementer prompt variant: every claim MUST carry a "
            "verbatim quote_span (no unquoted paraphrases). NOT replayable "
            "in v1 — new deepseek calls."
        ),
        "config_delta": {"prompt": {"require_quote_span": True}},
        "requires_replay": REPLAY_DEEPSEEK,
        "expected_direction": DIRECTION_UP,
    },
    {
        "id": "impl_route_threshold_1600",
        "stage": STAGE_IMPLEMENTER,
        "block": "routing",
        "description": (
            "Route threshold 2000 -> 1600 tokens: more docs take the "
            "deep-context extraction path. NOT replayable in v1 — the claims "
            "the alternate route would produce need new deepseek calls."
        ),
        "config_delta": {"route_threshold": 1600},
        "requires_replay": REPLAY_DEEPSEEK,
        "expected_direction": DIRECTION_PROBE,
    },
)


def default_variants() -> list[dict]:
    """The baseline variant set for the three stages (fresh copies).

    Callers may mutate their copy freely (nested ``config_delta`` maps
    included) — the module registry is never handed out, not even shallow
    copies of its nested dicts, so one round's edits cannot leak into the
    next (tests pin this)."""
    return [copy.deepcopy(variant) for variant in _REGISTRY]


def apply_variant(base_cfg: dict | None, variant: dict) -> dict:
    """Pure SHALLOW merge of the variant's ``config_delta`` over ``base_cfg``
    (the shipped decide.yaml gate block, or None for "all defaults").

    Shallow is deliberate and pinned: a nested dict in the delta (e.g. the
    4th-question variant's ``questions`` map) REPLACES the base's value
    under that key — config blocks are flat knob maps, not trees to deep-
    merge. Never mutates either input; always returns a new dict.
    """
    cfg = dict(base_cfg or {})
    delta = variant.get("config_delta") if isinstance(variant, dict) else None
    if isinstance(delta, dict):
        cfg.update(delta)
    return cfg

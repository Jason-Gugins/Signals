"""MiMo planner sub-stage: one LLM call per run produces a validated Plan.

The planner NARROWS AND ORDERS ONLY. One OpenRouter chat call turns the
ICP excerpt plus the account's allowed sources into an ordered subset of
those sources, optionally lowered budget knobs, and per-step acceptance
criteria. Every hard rule is enforced IN CODE (``validate_plan``), never
in the prompt: the planner may not invent sources, may not add fanout
sources the run did not include, and may not raise a budget knob above
its shipped value (config/sources.yaml, e.g. ``company_feed:
article_follow_max: 5``) — lowering only. Disallowed source ids are
PRUNED from the offending step (a step emptied by pruning is dropped, a
warning names the step and ids) so one bad step never invalidates the
whole plan — the validated plan only ever narrows.

Failure seam (same discipline as src/decide/jev.py): nothing here ever
raises past ``generate_plan`` — HTTP errors, timeouts, malformed JSON,
missing credentials and rejected plans all come back as
``(None, reason)`` so the deterministic plan remains the fallback and a
planner outage never fails the run. Credentials are never logged.

Spend guard: the validated plan is cached on disk keyed on
``sha256("{domain}|{profile}|{snapshot_hash}")`` so re-runs and identical
snapshots are reproducible and free; a dry run NEVER calls the LLM and
NEVER requires keys (it reuses the cache or falls back to the
deterministic plan).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
from loguru import logger

from src.decide.jev import DATA_EXIT_CONSENT
from src.decide.shapes import Plan, PlanStep

# The planner key lives in the untracked .env (never committed, never logged).
API_KEY_ENV = "OPENROUTER_API_KEY"
# The data-exit consent string is owned by the decide layer (shared verbatim).
CONSENT_ENV = "SIGNALS_DECIDE_DATA_EXIT"

DEFAULT_MODEL = "xiaomi/mimo-v2.6-pro"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_STEPS = 12
DEFAULT_TIMEOUT_S = 60.0
# data/ is the repo's gitignored data dir (precedent: data/antibot/).
DEFAULT_CACHE_DIR = Path("data/llm/plan_cache")


# ------------------------------------------------------------------- prompt


def build_prompt(
    icp_excerpt: str,
    domain: str,
    allowed_sources: list[str],
    knob_caps: dict[str, dict[str, int]],
    max_steps: int,
) -> str:
    """Build the planner prompt: pure, deterministic, secret-free.

    The prompt states the narrowing contract (never invent sources, never
    add fanout sources, never raise a knob) and the STRICT JSON output
    shape; enforcement still lives in ``validate_plan``, not here.
    """
    lines = [
        "You are the PLANNER for one signal-collection run.",
        "",
        f"Account domain: {domain}",
        f"ICP excerpt: {icp_excerpt}",
        "",
        "Allowed sources for this account (the ONLY source ids you may use):",
    ]
    for source in sorted(allowed_sources):
        caps = knob_caps.get(source) or {}
        cap_text = ", ".join(f"{k} (shipped cap {v})" for k, v in sorted(caps.items()))
        lines.append(f"- {source}: {cap_text or 'no budget knobs'}")
    lines += [
        "",
        f"Produce AT MOST {max_steps} ordered steps.",
        "",
        "You NARROW AND ORDER ONLY:",
        "- never invent sources; every source_ids entry must come from the allowed list above",
        "- never add fanout sources unless the run explicitly included them",
        (
            "- never raise a budget knob above its shipped cap; you may only LOWER knobs "
            "(omit a knob to leave it at the shipped value)"
        ),
        "- give acceptance criteria per step (each step must name the doc_ids it rests on)",
        "",
        (
            'Output STRICT JSON exactly matching: {"steps": [{"source_ids": ["<source>", ...], '
            '"budget_knobs": {"<source>": {"<knob>": <int>}}, "acceptance_criteria": ["..."]}]}'
        ),
        "No prose, no markdown fences, no api_key or secrets in the output.",
    ]
    return "\n".join(lines)


def _parse_content(content: object) -> dict | None:
    """Parse the LLM message content as JSON, stripping ``` fences if present."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        end = text.rfind("```")
        if end != -1:
            text = text[:end]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def call_planner(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict | None:
    """One POST /chat/completions call; returns the parsed plan dict or None.

    NEVER raises: HTTP errors, timeouts, transport failures and malformed
    content all return None (logged at warning). The api_key goes into the
    Authorization header only and is never logged.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = _parse_content(content)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("planner call failed: {}: {}", type(exc).__name__, exc)
        return None
    if parsed is None:
        logger.warning("planner returned malformed content")
    return parsed


# ----------------------------------------------------------------- validation


def validate_plan(
    raw: dict | None,
    *,
    allowed_sources: set[str],
    knob_caps: dict[str, dict[str, int]],
    max_steps: int,
    include_fanout: bool,
    fanout_keys: set[str],
) -> tuple[Plan | None, str]:
    """Hard validators IN CODE (not in the prompt).

    Returns (Plan, "ok") when the proposal validates untouched, (Plan,
    "pruned") when disallowed source ids had to be pruned from it, or
    (None, reject_reason) for structural failures — first failure wins,
    in order: shape -> step count -> source scope -> fanout widening ->
    budget knobs (lower-only) -> acceptance criteria. Step order is
    preserved: the plan narrows and orders.

    Source-scope violations PRUNE, never invalidate: invented ids (not in
    ``allowed_sources``) and fanout ids the run did not include are
    stripped from the offending step (a warning names the step and the
    pruned ids); a step emptied by pruning is dropped — its knobs and
    criteria go with it — and the plan survives with its remaining steps.
    Narrow-only: one bad step must not cost the whole plan. If EVERY step
    is pruned the honest outcome is a plan with zero steps; the caller
    (intel plan sub-stage) degrades to the deterministic scope.
    """
    # 1. shape: a dict with a "steps" list
    if not isinstance(raw, dict):
        return None, "malformed"
    steps = raw.get("steps")
    if not isinstance(steps, list):
        return None, "malformed"
    # 2. step count (the cap binds the PROPOSED plan's size)
    if len(steps) > max_steps:
        return None, "too_many_steps"
    # 3.+4. source scope and fanout widening, as PRUNING: invented ids and
    # fanout ids the run did not include are stripped from the step (never
    # a whole-plan rejection — both planner models trip one of these paths
    # on most runs, so rejecting here meant the planner never narrowed a
    # plan in production). A step emptied by pruning is dropped.
    pruned_any = False
    kept_steps: list[dict] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return None, "malformed"
        ids = step.get("source_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(s, str) for s in ids):
            return None, "malformed"
        invented = [sid for sid in ids if sid not in allowed_sources]
        if include_fanout:
            fanout_added = []
        else:
            fanout_added = [sid for sid in ids if sid in fanout_keys and sid not in invented]
        strip = set(invented) | set(fanout_added)
        if not strip:
            kept_steps.append(step)
            continue
        pruned_any = True
        kept = [sid for sid in ids if sid not in strip]
        logger.warning(
            "planner: pruned disallowed source ids {} from proposed step {} "
            "(invented: {}, fanout-excluded: {}); {}",
            sorted(strip),
            index,
            invented,
            fanout_added,
            f"step kept as {kept}" if kept else "step dropped (no sources left)",
        )
        if not kept:
            continue
        step = dict(step, source_ids=kept)
        knobs = step.get("budget_knobs", {})
        if isinstance(knobs, dict):
            # a knob attached to a pruned source must go with the source —
            # a leftover entry would reject the step (unknown_knob) and
            # kill the plan through the back door
            step = dict(step, budget_knobs={s: k for s, k in knobs.items() if s not in strip})
        kept_steps.append(step)
    # 5. budget knobs: a plan may only LOWER a knob, never raise it
    for step in kept_steps:
        knobs = step.get("budget_knobs", {})
        if not isinstance(knobs, dict):
            return None, "malformed"
        for source, entries in knobs.items():
            if (
                source not in step["source_ids"]
                or source not in knob_caps
                or not isinstance(entries, dict)
            ):
                return None, f"unknown_knob:{source}"
            for knob, value in entries.items():
                if knob not in knob_caps[source]:
                    return None, f"unknown_knob:{source}.{knob}"
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    return None, "malformed"
                if value > knob_caps[source][knob]:
                    return None, f"budget_raise:{source}.{knob}"
    # 6. acceptance criteria: a list of strings (empty list allowed)
    for step in kept_steps:
        criteria = step.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or any(not isinstance(c, str) for c in criteria):
            return None, "malformed"
    plan = Plan(
        steps=[
            PlanStep(
                source_ids=list(step["source_ids"]),
                budget_knobs={s: dict(k) for s, k in step.get("budget_knobs", {}).items()},
                acceptance_criteria=list(step.get("acceptance_criteria", [])),
            )
            for step in kept_steps
        ]
    )
    return plan, ("pruned" if pruned_any else "ok")


# ----------------------------------------------------------------- plan cache


def _cache_path(cache_dir: Path, domain: str, profile: str, snapshot_hash: str) -> Path:
    key = hashlib.sha256(f"{domain}|{profile}|{snapshot_hash}".encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{key}.json"


def _plan_to_dict(plan: Plan) -> dict:
    return {
        "steps": [
            {
                "source_ids": list(step.source_ids),
                "budget_knobs": {s: dict(k) for s, k in step.budget_knobs.items()},
                "acceptance_criteria": list(step.acceptance_criteria),
            }
            for step in plan.steps
        ]
    }


def cache_plan(cache_dir: Path, domain: str, profile: str, snapshot_hash: str, plan: Plan) -> None:
    """Persist a validated plan as a plain dict keyed on the run triple."""
    path = _cache_path(cache_dir, domain, profile, snapshot_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plan_to_dict(plan)), encoding="utf-8")


def load_cached_plan(cache_dir: Path, domain: str, profile: str, snapshot_hash: str) -> Plan | None:
    """Read a cached plan; corrupt or missing files degrade to None."""
    path = _cache_path(cache_dir, domain, profile, snapshot_hash)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Plan(
            steps=[
                PlanStep(
                    source_ids=list(step["source_ids"]),
                    budget_knobs={s: dict(k) for s, k in step["budget_knobs"].items()},
                    acceptance_criteria=list(step["acceptance_criteria"]),
                )
                for step in data["steps"]
            ]
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------- entry point


def _max_steps(planner_cfg: dict) -> int:
    """``planner.max_plan_steps`` as an int; the default when absent/bad.

    Never raises: a malformed config value must not turn the spend guard
    into a crash.
    """
    try:
        return int(planner_cfg.get("max_plan_steps", DEFAULT_MAX_STEPS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_STEPS


def generate_plan(
    *,
    decide_cfg: dict,
    domain: str,
    profile: str,
    snapshot_hash: str,
    icp_excerpt: str,
    allowed_sources: set[str],
    knob_caps: dict[str, dict[str, int]],
    fanout_keys: set[str],
    include_fanout: bool,
    dry_run: bool,
    cache_dir: Path | None = None,
) -> tuple[Plan | None, str]:
    """Orchestrator-facing planner entry: (Plan | None, reason).

    Reason is one of: "cache", "dry_run", "missing_credentials",
    "llm_error", "generated", "generated_pruned" (the LLM plan needed
    disallowed ids pruned — possibly down to zero steps), or a
    validate_plan reject reason. Missing keys/consent and LLM failures
    never raise — the deterministic plan remains the run's fallback.
    """
    cache = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    planner_cfg = decide_cfg.get("planner") or {}
    use_cache = planner_cfg.get("plan_cache", True) is not False
    max_steps = _max_steps(planner_cfg)

    # 1. spend guard: a cached validated plan makes the run reproducible and
    # free — but a cache hit is re-validated against the CURRENT inputs
    # (allowed sources, knob caps, fanout inclusion) before it is served: a
    # config change must never let a stale plan reintroduce out-of-scope
    # sources. A cache that no longer validates CLEANLY — wrong shape,
    # raised caps, or ids that would now be PRUNED (the scope changed since
    # caching, so the planner should reconsider it, not serve a narrowed
    # relic) — is a miss: the stale file is dropped and the plan is
    # regenerated below.
    if use_cache:
        cached = load_cached_plan(cache, domain, profile, snapshot_hash)
        if cached is not None:
            plan, why = validate_plan(
                _plan_to_dict(cached),
                allowed_sources=allowed_sources,
                knob_caps=knob_caps,
                max_steps=max_steps,
                include_fanout=include_fanout,
                fanout_keys=fanout_keys,
            )
            if plan is not None and why == "ok":
                return plan, "cache"
            try:
                _cache_path(cache, domain, profile, snapshot_hash).unlink()
            except OSError:
                pass
    # 2. dry-run's no-fetch contract: never call the LLM, never require keys
    if dry_run:
        return None, "dry_run"
    # 3. credentials: key + verbatim data-exit consent, else deterministic fallback
    api_key = os.environ.get(API_KEY_ENV)
    consent = os.environ.get(CONSENT_ENV)
    if not api_key or consent != DATA_EXIT_CONSENT:
        logger.debug(
            "planner: missing {} or {} consent -> deterministic plan", API_KEY_ENV, CONSENT_ENV
        )
        return None, "missing_credentials"
    # 4. planner settings from config (defaults shipped in config/decide.yaml)
    model = planner_cfg.get("model", DEFAULT_MODEL)
    base_url = planner_cfg.get("base_url", DEFAULT_BASE_URL)
    timeout_s = float(planner_cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
    # 5. one LLM call
    prompt = build_prompt(icp_excerpt, domain, sorted(allowed_sources), knob_caps, max_steps)
    raw = call_planner(prompt, model=model, base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    if raw is None:
        return None, "llm_error"
    # 6. validate IN CODE (source-scope violations are pruned, not rejected);
    # a structurally rejected plan is never cached
    plan, why = validate_plan(
        raw,
        allowed_sources=allowed_sources,
        knob_caps=knob_caps,
        max_steps=max_steps,
        include_fanout=include_fanout,
        fanout_keys=fanout_keys,
    )
    if plan is None:
        return None, why
    # 7. cache the validated (possibly pruned) plan — the pruned form
    # validates cleanly forever after, so later runs hit the cache
    if use_cache:
        cache_plan(cache, domain, profile, snapshot_hash, plan)
    return plan, ("generated_pruned" if why == "pruned" else "generated")

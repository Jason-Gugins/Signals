"""Tests for the `intel` CLI command wiring (Task 13).

The command is a thin shell over ``src.pipeline.intel.run_intel``: it forwards
every flag, prints the per-stage report deterministically (STAGES order), the
written package paths, the gap count and any per-stage errors, and it turns the
coordinator's ``ValueError`` refusals into a non-zero exit.

No network, no filesystem writes: the coordinator is replaced with a recording
double, the orchestrator factory is replaced with a sentinel, and every result
is a plain dict in the documented shape.
"""

from __future__ import annotations

from click.testing import CliRunner

import src.cli as cli_mod
import src.pipeline.intel as intel_mod
from src.pipeline.intel import STAGES

DOMAIN = "acme.com"

#: Stand-in orchestrator; proves the command passes the lazy factory's product
#: (or at least asks for it) instead of building one inside the command.
SENTINEL_ORCH = object()


def _stub_result(**overrides) -> dict:
    """A run_intel-shaped result with all five stages ran."""
    result = {
        "domain": DOMAIN,
        "created": True,
        "stages": {name: {"status": "ran"} for name in STAGES},
        "errors": {},
        "coverage": [],
        "gaps": [],
        "dossier": None,
        "paths": {},
    }
    result.update(overrides)
    return result


def _patch(monkeypatch, outcome):
    """Patch the two seams; return the list of captured (args, kwargs)."""
    calls: list = []

    def fake_run_intel(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(outcome, BaseException) or (
            isinstance(outcome, type) and issubclass(outcome, BaseException)
        ):
            raise outcome
        return outcome

    monkeypatch.setattr(cli_mod, "Orchestrator", lambda cfg: SENTINEL_ORCH)
    monkeypatch.setattr(intel_mod, "run_intel", fake_run_intel)
    return calls


def test_cli_runs_and_prints_every_stage(monkeypatch):
    _patch(monkeypatch, _stub_result())

    result = CliRunner().invoke(cli_mod.main, ["intel", DOMAIN])

    assert result.exit_code == 0, result.output
    for stage in STAGES:
        assert f"{stage}: ran" in result.output, stage


def test_cli_forwards_flags_correctly(monkeypatch):
    calls = _patch(monkeypatch, _stub_result())

    result = CliRunner().invoke(
        cli_mod.main,
        [
            "intel",
            DOMAIN,
            "--name",
            "Acme Inc",
            "--market-profile",
            "canada-software",
            "--skip",
            "identity",
            "--skip",
            "score",
            "--force",
            "--with-marketplaces",
            "--with-linkedin-resolve",
            "--no-write",
            "--max-signals",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (DOMAIN,)
    assert kwargs["name"] == "Acme Inc"
    assert kwargs["market_profile_id"] == "canada-software"
    assert kwargs["skip"] == ("identity", "score")
    assert isinstance(kwargs["skip"], tuple)
    assert len(kwargs["skip"]) == 2
    assert kwargs["force"] is True
    assert kwargs["with_marketplaces"] is True
    assert kwargs["with_linkedin_resolve"] is True
    assert kwargs["write"] is False
    assert kwargs["max_signals"] == 7
    # Defaults the command also pins explicitly (never left to the callee).
    assert kwargs["dry_run"] is False
    assert kwargs["config"] is not None
    assert kwargs["orch"] is SENTINEL_ORCH


def test_cli_refusal_exits_non_zero(monkeypatch):
    _patch(
        monkeypatch,
        ValueError("dry-run requires an existing account: acme.com is not in the registry"),
    )

    result = CliRunner().invoke(cli_mod.main, ["intel", DOMAIN])

    assert result.exit_code != 0
    assert "intel refused:" in result.output


def test_cli_prints_package_paths_and_gap_count(monkeypatch):
    _patch(
        monkeypatch,
        _stub_result(
            paths={
                "package_dir": "/out/acme.com-20260914T000000Z-abc",
                "manifest": "/out/acme.com-20260914T000000Z-abc/manifest.json",
            },
            gaps=["no relevance vocabulary configured", "identity stage failed: boom"],
        ),
    )

    result = CliRunner().invoke(cli_mod.main, ["intel", DOMAIN])

    assert result.exit_code == 0, result.output
    assert "wrote: /out/acme.com-20260914T000000Z-abc" in result.output
    assert "wrote: /out/acme.com-20260914T000000Z-abc/manifest.json" in result.output
    assert "gaps: 2" in result.output


def test_group_dry_run_flag_reaches_the_module(monkeypatch):
    calls = _patch(monkeypatch, _stub_result())

    result = CliRunner().invoke(cli_mod.main, ["--dry-run", "intel", DOMAIN])

    assert result.exit_code == 0, result.output
    assert calls[0][1]["dry_run"] is True


def test_cli_reports_errors(monkeypatch):
    stages = {name: {"status": "ran"} for name in STAGES}
    stages["collect"] = {"status": "failed", "reason": "connection reset"}
    _patch(
        monkeypatch,
        _stub_result(stages=stages, errors={"collect": "connection reset"}),
    )

    result = CliRunner().invoke(cli_mod.main, ["intel", DOMAIN])

    assert result.exit_code == 0, result.output
    assert "collect" in result.output
    assert "connection reset" in result.output
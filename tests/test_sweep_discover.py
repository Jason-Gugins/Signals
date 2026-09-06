"""Plan T4 — CLI wiring for `sweep --discover NAME` (+ `--ddg`).

Follows the repo CLI-test pattern (tests/test_cli_pipeline.py /
tests/test_sweep.py): class-level Orchestrator patches, never a seeded
ctx.obj. The discovery path bypasses parse_target and run_sweep entirely —
no account creation anywhere in it.
"""

from __future__ import annotations

from click.testing import CliRunner


def _fake_result(
    name="Acme Corp",
    status="ambiguous",
    domain=None,
    candidates=None,
    agreement=None,
    queued=True,
) -> dict:
    result = {
        "name": name,
        "status": status,
        "domain": domain,
        "candidates": candidates
        if candidates is not None
        else [
            {"domain": "acme.com", "score": 4, "stage": "wikidata", "label": "Acme Corp",
             "url": "https://acme.com"},
            {"domain": "acme.io", "score": 1, "stage": "ddg", "title": "Acme — official site",
             "url": "https://acme.io"},
        ],
        "stages": {
            "wikidata": {"status": "ambiguous"},
            "wikipedia": {"status": "skipped"},
            "gkg": {"status": "unconfigured"},
        },
        "queued": queued,
    }
    if agreement:
        result["agreement"] = agreement
    return result


def _patch_discover(monkeypatch, results=None, error_for=None):
    """Class-level Orchestrator.discover patch (Boom-contract style): records
    calls; returns canned results by name (a dict or a callable returning one,
    else a shared default)."""
    from src.pipeline.orchestrator import Orchestrator

    calls = []

    def fake_discover(self, name, ddg=False):
        calls.append({"name": name, "ddg": ddg})
        if error_for and name in error_for:
            raise error_for[name]
        table = results() if callable(results) else results
        if isinstance(table, dict):
            return table[name]
        return _fake_result(name=name)

    monkeypatch.setattr(Orchestrator, "discover", fake_discover)
    return calls


def _setup(monkeypatch):
    from src.pipeline.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "__init__", lambda self, *a, **k: None)


def _forbid_run_sweep(monkeypatch):
    from src.pipeline import sweep as sweep_mod

    def boom(*a, **kw):
        raise AssertionError("run_sweep must not run in --discover mode")

    monkeypatch.setattr(sweep_mod, "run_sweep", boom)


# ── happy paths ─────────────────────────────────────────────────────────────

def test_cli_sweep_discover_prints_ranked_candidates_and_promote_hint(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert calls == [{"name": "Acme Corp", "ddg": False}]
    assert "Acme Corp: ambiguous (2 candidates)" in result.output
    # Ranked report line: score, stage, domain, label, url.
    assert "4\twikidata\tacme.com\tAcme Corp\thttps://acme.com" in result.output
    assert "1\tddg\tacme.io\tAcme — official site\thttps://acme.io" in result.output
    assert "queued to identity_candidates; promote with: sweep acme.com" in result.output


def test_cli_sweep_discover_resolved_agreement_report(monkeypatch):
    from src.cli import main

    monkeypatch.setattr(
        "src.pipeline.orchestrator.Orchestrator.discover",
        lambda self, name, ddg=False: _fake_result(
            name=name, status="resolved", domain="acme.com",
            agreement=["wikidata", "gkg"],
            candidates=[
                {"domain": "acme.com", "score": 2, "stage": "wikidata", "label": "Acme"},
            ],
        ),
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert "Acme Corp: resolved -> acme.com (agreement: wikidata + gkg)" in result.output
    assert "queued to identity_candidates; promote with: sweep acme.com" in result.output


def test_cli_sweep_discover_bare_name_bypasses_parse_target(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    # No dot in the name: the sweep refusal must not fire in --discover mode.
    result = CliRunner().invoke(main, ["sweep", "--discover", "Glow Security"])

    assert result.exit_code == 0, result.output
    assert calls[0]["name"] == "Glow Security"
    assert "refused" not in result.output


def test_cli_sweep_discover_accepts_multiple_names(monkeypatch):
    from src.cli import main

    calls = _patch_discover(
        monkeypatch,
        results=lambda: {
            "Acme": _fake_result(name="Acme", status="no_match", candidates=[]),
            "Globex": _fake_result(name="Globex", status="no_match", candidates=[]),
        },
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme", "--discover", "Globex"])

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in calls] == ["Acme", "Globex"]
    assert "Acme: no_match (0 candidates)" in result.output
    assert "Globex: no_match (0 candidates)" in result.output


def test_cli_sweep_discover_ddg_flag_flows_through(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme", "--ddg"])
    assert result.exit_code == 0, result.output
    assert calls[-1]["ddg"] is True

    CliRunner().invoke(main, ["sweep", "--discover", "Acme"])
    assert calls[-1]["ddg"] is False  # default off


def test_cli_sweep_discover_per_name_failure_isolated(monkeypatch):
    from src.cli import main

    calls = _patch_discover(
        monkeypatch,
        results=lambda: {
            "Bad": None,
            "Good": _fake_result(name="Good", status="no_match", candidates=[]),
        },
        error_for={"Bad": RuntimeError("boom")},
    )
    _forbid_run_sweep(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "--discover", "Bad", "--discover", "Good"])

    assert result.exit_code == 0, result.output
    assert [c["name"] for c in calls] == ["Bad", "Good"]
    assert "Bad: discovery failed: boom" in result.output
    assert "Good: no_match (0 candidates)" in result.output


# ── refusals / guardrails ───────────────────────────────────────────────────

def test_cli_sweep_discover_never_creates_accounts(monkeypatch):
    from src.cli import main
    from src.identity.registry import AccountRegistry

    _patch_discover(monkeypatch)
    _setup(monkeypatch)
    upserts = []
    monkeypatch.setattr(
        AccountRegistry, "upsert", lambda self, account, **kw: upserts.append(account)
    )

    result = CliRunner().invoke(main, ["sweep", "--discover", "Acme Corp"])

    assert result.exit_code == 0, result.output
    assert upserts == []


def test_cli_sweep_discover_conflicts_with_positional_target(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep", "acme.io", "--discover", "Acme"])

    assert result.exit_code != 0
    assert "not both" in result.output
    assert calls == []


def test_cli_sweep_still_requires_target_without_discover(monkeypatch):
    from src.cli import main

    calls = _patch_discover(monkeypatch)
    _setup(monkeypatch)

    result = CliRunner().invoke(main, ["sweep"])

    assert result.exit_code != 0
    assert "URL_OR_NAME" in result.output
    assert calls == []

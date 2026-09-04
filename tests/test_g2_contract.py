"""G2 slug resolution contract (P2 Task 7): candidates-only, never guesses.

Mirrors the house contract enforced by ``src/identity/capterra_resolve.py``:
exact case-insensitive match -> normalized match -> unique substring match
(either direction) -> otherwise NO slug, with the candidate slugs surfaced.
The old behavior silently accepted ``search_results[0]`` on ambiguity, which
burned anti-bot budget on another company's G2 reviews.
"""

from __future__ import annotations

from src.core.config import Config
from src.core.models import Account
from src.identity.g2_resolve import (
    parse_g2_search_results,
    resolve_g2_from_results,
    resolve_g2_slug,
)
from src.pipeline.orchestrator import Orchestrator


def _g2_html(*pairs) -> str:
    rows = "".join(
        f'<a href="/products/{slug}/reviews" class="product-listing__link">'
        f'<div class="product-card__product-name">{name}</div></a>'
        for slug, name in pairs
    )
    return f"<html><body><div class='product-listing'>{rows}</div></body></html>"


# --- resolver contract -----------------------------------------------------


def test_exact_match_case_insensitive_accepted():
    results = parse_g2_search_results(
        _g2_html(("slack", "Slack"), ("slack-technologies", "Slack Technologies"))
    )
    r = resolve_g2_from_results("SLACK", results)
    assert r.status == "resolved" and r.slug == "slack"
    assert resolve_g2_slug("SLACK", results) == "slack"


def test_normalized_name_match_ignores_punctuation_and_spacing():
    results = parse_g2_search_results(_g2_html(("acme-corp", "Acme  Corp!"), ("other", "Other Tool")))
    assert resolve_g2_slug("Acme Corp", results) == "acme-corp"


def test_unique_substring_either_direction_accepted():
    # target is a substring of exactly one candidate name
    results = parse_g2_search_results(_g2_html(("acme-corp", "Acme Corp"), ("zeta", "Zeta Tools")))
    r = resolve_g2_from_results("Acme", results)
    assert r.status == "resolved" and r.slug == "acme-corp"
    # candidate name is a substring of the target
    results2 = parse_g2_search_results(_g2_html(("acme", "Acme"), ("zeta", "Zeta Tools")))
    assert resolve_g2_slug("Acme Corp Inc", results2) == "acme"


def test_two_candidate_substring_is_ambiguous_no_slug():
    # "Acme" is a substring of TWO candidates -> ambiguous, never a winner
    results = parse_g2_search_results(
        _g2_html(("acme-corp", "Acme Corp"), ("acme-robotics", "Acme Robotics"))
    )
    r = resolve_g2_from_results("Acme", results)
    assert r.slug is None
    assert r.status == "ambiguous"
    assert [c["slug"] for c in r.candidates] == ["acme-corp", "acme-robotics"]
    # backward-compatible wrapper never guesses either
    assert resolve_g2_slug("Acme", results) is None


def test_loose_candidates_only_is_no_match_with_candidates_surfaced():
    results = parse_g2_search_results(_g2_html(("unrelated", "Unrelated Tool"), ("other", "Other")))
    r = resolve_g2_from_results("Acme", results)
    assert r.slug is None
    assert r.status == "no_match"
    assert len(r.candidates) == 2
    assert resolve_g2_slug("Acme", results) is None


def test_empty_results_return_none():
    r = resolve_g2_from_results("Acme", [])
    assert r.slug is None
    assert r.status == "no_match"
    assert r.candidates == []
    assert resolve_g2_slug("Acme", []) is None


# --- orchestrator call site -------------------------------------------------


class _G2Fetch:
    def __init__(self, html: str):
        self.body = html.encode("utf-8")

    def get(self, task, **kw):
        from src.core.http import FetchResult

        from src.core.models import Document

        doc = Document(
            doc_id="g2", source=task.source, url=task.url, domain=task.domain,
            body=self.body, status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _LogCapture:
    """Stands in for the orchestrator's logger and records warnings."""

    def __init__(self):
        self.warnings: list[str] = []

    def _noop(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        try:
            self.warnings.append(msg.format(*a, **k) if a else str(msg))
        except Exception:
            self.warnings.append(str(msg))

    def __getattr__(self, name):
        return self._noop


def _orch(tmp_path, fetcher, log):
    cfg = Config()
    cfg.contact_email = "recon@example.com"
    cfg.http.respect_robots = False
    cfg.storage.db_path = str(tmp_path / "s.db")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.briefs_dir = str(tmp_path / "briefs")
    cfg.storage.export_dir = str(tmp_path / "exports")
    cfg.config_dir = "config"
    return Orchestrator(cfg, fetcher=fetcher)


def test_orchestrator_persists_unique_g2_slug(tmp_path):
    orch = _orch(
        tmp_path,
        _G2Fetch(_g2_html(("acme-corp", "Acme Corp"), ("zeta", "Zeta Tools"))),
        _LogCapture(),
    )
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False, g2=True)
    assert out["g2"] == 1
    assert orch.registry.get("acme.com").g2_slug == "acme-corp"


def test_orchestrator_ambiguous_slug_persists_nothing_and_reports_candidates(tmp_path, monkeypatch):
    log = _LogCapture()
    monkeypatch.setattr("src.pipeline.orchestrator.logger", log)
    orch = _orch(
        tmp_path,
        _G2Fetch(_g2_html(("acme-corp", "Acme Corp"), ("acme-robotics", "Acme Robotics"))),
        log,
    )
    orch.registry.upsert(Account(domain="acme.com", name="Acme"))
    out = orch.resolve(ats=False, cik=False, feeds=False, icp=False, g2=True)
    assert out["g2"] == 0
    # nothing persisted on ambiguity
    assert orch.registry.get("acme.com").g2_slug is None
    # candidates surfaced instead of a guess
    joined = "\n".join(log.warnings)
    assert "acme-corp" in joined and "acme-robotics" in joined

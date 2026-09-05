"""Task 15 — BBB rating delta: letter-grade downgrade / accreditation loss.

The BBB profile already carries ``bbb_rating`` (letter grade) and the
accreditation flag, but nothing enabled a cycle-over-cycle diff. This wave:

- persists the observation on the account (``extra_data['bbb_rating']``,
  ``extra_data['bbb_accredited']``, ``extra_data['bbb_observed_at']``) through
  the SAME ``AccountRegistry`` the runner injects into parse ``task_meta``
  (``meta.setdefault("registry", ...)`` in ``CollectorRunner._run_pair``) —
  merged, never clobbering other ``extra_data`` keys (``bbb_url`` must
  survive or the next ``plan()`` emits nothing);
- emits exactly one ``reputation_drop`` per parse on a letter-grade
  downgrade (any step down the A+ … F scale) or an accreditation loss
  (True -> False). Improvements, no change, and first observations
  (no baseline — never guess) emit nothing.

Covers the pure grade-ordinal helper, the baseline/delta rules through the
real adapter ``parse``, and a ``normalize_batch`` persistence test pinning
that ``reputation_drop`` survives normalization (taxonomy entry was
pre-registered in 6e34a10).
"""

from __future__ import annotations

from pathlib import Path

from src.core.db import Database
from src.core.models import Account, Document
from src.identity.registry import AccountRegistry
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.bbb import BbbProfileSource, grade_ordinal

FIXTURE = Path(__file__).parent / "fixtures" / "bbb" / "bbb_avalara_profile.html"
BBB_URL = "https://www.bbb.org/us/wa/seattle/profile/computer-software-developers/avalara-inc-1296-22018273"
TODAY = "2026-09-05"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _graded(html: str, grade: str) -> str:
    """Fixture surgery: swap the letter grade, leaving everything else real."""
    out = html.replace(
        '<span class="bpr-header-rating">A+</span>',
        f'<span class="bpr-header-rating">{grade}</span>',
    )
    assert out != html or grade == "A+", "rating span not found in fixture"
    return out


def _unaccredited(html: str) -> str:
    """Fixture surgery: flip the accreditation flag to false."""
    out = html.replace('data-accredited="true"', 'data-accredited="false"')
    assert out != html, "accreditation attribute not found in fixture"
    return out


def _rating_stripped(html: str) -> str:
    """Fixture surgery: a profile page that renders NO rating span at all."""
    out = html.replace('<span class="bpr-header-rating">A+</span>', "")
    assert out != html, "rating span not found in fixture"
    return out


def _doc(html: str) -> Document:
    return Document(
        doc_id="bbb-delta-doc",
        source="bbb_profile",
        url=BBB_URL,
        body=html.encode("utf-8"),
    )


def _registry(tmp_path) -> AccountRegistry:
    return AccountRegistry(Database(tmp_path / "reg.db"))


def _account(registry: AccountRegistry) -> Account:
    """Seed the account the way the runner sees it (canonical, from registry)."""
    return registry.upsert(
        Account(domain="avalara.com", name="Avalara Inc", extra_data={"bbb_url": BBB_URL})
    )


def _parse(registry, account, html):
    return BbbProfileSource().parse(_doc(html), account, {"registry": registry, "today": TODAY})


def _drops(cands) -> list:
    return [c for c in cands if c.signal_type == "reputation_drop"]


# ── pure grade ordinal helper ────────────────────────────────────────────────


def test_grade_ordinal_is_ordered_best_to_worst():
    scale = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]
    ordinals = [grade_ordinal(g) for g in scale]
    assert all(o is not None for o in ordinals)
    assert ordinals == sorted(ordinals)
    assert ordinals[0] < ordinals[-1]


def test_grade_ordinal_rejects_unknown_and_non_string():
    assert grade_ordinal("NR") is None
    assert grade_ordinal("") is None
    assert grade_ordinal("A++") is None
    assert grade_ordinal(None) is None
    assert grade_ordinal(3) is None


# ── first observation: baseline persisted, nothing emitted ──────────────────


def test_first_observation_persists_baseline_without_candidate(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)

    cands = _parse(registry, account, _html())

    assert cands == []
    stored = registry.get("avalara.com")
    assert stored.extra_data["bbb_rating"] == "A+"
    assert stored.extra_data["bbb_accredited"] is True
    assert stored.extra_data["bbb_observed_at"] == TODAY
    # Sparse upsert must NOT clobber unrelated extra_data keys (bbb_url is
    # what keeps plan() emitting tasks at all).
    assert stored.extra_data["bbb_url"] == BBB_URL


def test_parse_without_registry_emits_and_persists_nothing(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)

    cands = BbbProfileSource().parse(_doc(_html()), account, {})

    assert cands == []
    stored = registry.get("avalara.com")
    assert "bbb_rating" not in stored.extra_data


# ── downgrade: exactly one reputation_drop ───────────────────────────────────


def test_downgrade_emits_exactly_one_reputation_drop(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    assert _parse(registry, account, _html()) == []  # baseline A+

    cands = _parse(registry, account, _graded(_html(), "B"))

    drops = _drops(cands)
    assert len(drops) == 1
    d = drops[0]
    assert d.natural_key == "bbbdrop:avalara.com:2026-09"
    assert d.observed_at == TODAY
    assert d.title == "BBB A+ → B"
    assert d.confidence == 0.7
    assert d.domain_override == "avalara.com"
    assert d.evidence_data["old_rating"] == "A+"
    assert d.evidence_data["new_rating"] == "B"
    assert d.evidence_data["accredited_before"] is True
    assert d.evidence_data["accredited_after"] is True
    # The baseline advanced: the NEXT cycle diffs against B, not A+.
    assert registry.get("avalara.com").extra_data["bbb_rating"] == "B"


def test_multi_step_downgrade_also_fires(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline A+

    drops = _drops(_parse(registry, account, _graded(_html(), "C-")))

    assert len(drops) == 1
    assert drops[0].evidence_data["old_rating"] == "A+"
    assert drops[0].evidence_data["new_rating"] == "C-"


# ── improvement / no change: silent ──────────────────────────────────────────


def test_improvement_and_same_grade_emit_nothing(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _graded(_html(), "B"))  # baseline B

    assert _drops(_parse(registry, account, _graded(_html(), "A"))) == []
    # Improvement still persists the new baseline.
    assert registry.get("avalara.com").extra_data["bbb_rating"] == "A"

    assert _drops(_parse(registry, account, _graded(_html(), "A"))) == []


def test_missing_rating_markup_never_erases_baseline(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline A+

    # Next cycle's page stops rendering the rating span: no candidate, and
    # the stored baseline survives for the cycle after.
    assert _drops(_parse(registry, account, _rating_stripped(_html()))) == []
    assert registry.get("avalara.com").extra_data["bbb_rating"] == "A+"


# ── accreditation loss ───────────────────────────────────────────────────────


def test_accreditation_loss_with_same_grade_emits_candidate(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline: accredited, A+

    drops = _drops(_parse(registry, account, _unaccredited(_html())))

    assert len(drops) == 1
    d = drops[0]
    assert d.natural_key == "bbbdrop:avalara.com:2026-09"
    assert d.evidence_data["old_rating"] == "A+"
    assert d.evidence_data["new_rating"] == "A+"
    assert d.evidence_data["accredited_before"] is True
    assert d.evidence_data["accredited_after"] is False


def test_accreditation_gain_does_not_emit(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _unaccredited(_html()))  # baseline: not accredited

    drops = _drops(_parse(registry, account, _html()))  # gains accreditation

    assert drops == []
    assert registry.get("avalara.com").extra_data["bbb_accredited"] is True


# ── persistence: reputation_drop survives normalize_batch ────────────────────


def test_reputation_drop_persists_through_normalize_batch(tmp_path):
    registry = _registry(tmp_path)
    account = _account(registry)
    _parse(registry, account, _html())  # baseline A+
    (drop,) = _drops(_parse(registry, account, _graded(_html(), "B")))

    # The real adapter key, pinned so the source column stays truthful.
    assert BbbProfileSource.key == "bbb_profile"
    valid, rejected = normalize_batch(
        [drop],
        account=account,
        source=BbbProfileSource.key,
        taxonomy=Taxonomy.load(),
        now=f"{TODAY}T00:00:00Z",
    )

    assert rejected == []
    assert len(valid) == 1
    sig = valid[0]
    assert sig.signal_type == "reputation_drop"
    assert sig.category == "negative"
    assert sig.polarity == "negative"
    assert sig.source == "bbb_profile"
    # The natural key is folded into signal_id: domain + type + natural_key.
    assert sig.signal_id == make_signal_id(
        "avalara.com", "reputation_drop", "bbbdrop:avalara.com:2026-09"
    )

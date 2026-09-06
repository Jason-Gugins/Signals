"""funding_drought (plan Task 10): a prior ``funding_form_d`` raise gone
18-24 months silent emits the pre-registered ``funding_drought`` type
(negative, runway pressure) for the account.

Pure helper: ``drought_candidates()`` in ``src/pipeline/funding.py`` turns the
account's persisted ``funding_form_d`` observed_at dates into at most ONE
monthly-deduped candidate when the most recent raise is 18-24 months old
(older is too stale to be news; newer is not a drought yet; never funded is
never a drought). Runner wiring: the per-account pass queries the signals
table for the account's ``funding_form_d`` observed_at dates and persists
through ``self._persist(account, "sec_formd", ...)``, deduped by the monthly
natural key on re-runs.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timezone

from src.core.config import Config
from src.core.db import Database
from src.core.http import FetchResult
from src.core.models import Account, Document
from src.core.rawstore import RawStore
from src.core.runlog import RunContext
from src.identity.registry import AccountRegistry
from src.pipeline.funding import drought_candidates
from src.pipeline.runner import CollectorRunner
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.store import SignalStore
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter

TODAY = date(2026, 9, 4)
DOMAIN = "acme.com"


def months_ago(today: date, n: int) -> date:
    """n calendar months before ``today``, clamped to the target month's end."""
    total = today.year * 12 + (today.month - 1) - n
    y, m0 = divmod(total, 12)
    m = m0 + 1
    return date(y, m, min(today.day, calendar.monthrange(y, m)[1]))


def rows(*observed: date) -> list[dict]:
    return [{"observed_at": d.isoformat()} for d in observed]


# ── Pure helper (injected today — never the clock) ─────────────────────────


def test_unit_18mo_silence_emits_one_candidate():
    cands = drought_candidates(
        rows(months_ago(TODAY, 18)), domain=DOMAIN, today=TODAY.isoformat()
    )
    assert len(cands) == 1
    cand = cands[0]
    assert cand.signal_type == "funding_drought"
    assert cand.natural_key == f"drought:{DOMAIN}:2026-09"
    assert cand.observed_at == TODAY.isoformat()
    assert cand.title == "No fresh raise in 18 months"
    assert cand.confidence == 0.6
    assert cand.evidence_data == {
        "last_funding": months_ago(TODAY, 18).isoformat(),
        "months_silent": 18,
    }


def test_unit_recent_raise_is_not_a_drought():
    # 6 months of silence: the prior raise is still fresh.
    assert drought_candidates(rows(months_ago(TODAY, 6)), domain=DOMAIN, today=TODAY.isoformat()) == []


def test_unit_over_24mo_is_too_stale_to_be_news():
    assert drought_candidates(rows(months_ago(TODAY, 30)), domain=DOMAIN, today=TODAY.isoformat()) == []


def test_unit_never_funded_or_unparseable_is_silent():
    assert drought_candidates([], domain=DOMAIN, today=TODAY.isoformat()) == []
    assert drought_candidates([{"observed_at": None}, {"observed_at": "garbage"}], domain=DOMAIN, today=TODAY.isoformat()) == []


def test_unit_most_recent_raise_drives_the_window():
    # 36mo + 19mo rows: the most recent (19mo) lands in the window -> emit.
    cands = drought_candidates(
        rows(months_ago(TODAY, 36), months_ago(TODAY, 19)),
        domain=DOMAIN,
        today=TODAY.isoformat(),
    )
    assert len(cands) == 1
    assert cands[0].evidence_data["last_funding"] == months_ago(TODAY, 19).isoformat()
    assert cands[0].evidence_data["months_silent"] == 19
    # 36mo + 7mo rows: the most recent (7mo) is fresh -> silent.
    assert drought_candidates(
        rows(months_ago(TODAY, 36), months_ago(TODAY, 7)),
        domain=DOMAIN,
        today=TODAY.isoformat(),
    ) == []


def test_unit_monthly_natural_key_dedupes_within_a_month():
    same_month = [
        drought_candidates(rows(months_ago(TODAY, 18)), domain=DOMAIN, today=t)[0].natural_key
        for t in ("2026-09-04", "2026-09-28")
    ]
    assert same_month == [f"drought:{DOMAIN}:2026-09"] * 2
    next_month = drought_candidates(
        rows(months_ago(TODAY, 18)), domain=DOMAIN, today="2026-10-01"
    )[0].natural_key
    assert next_month == f"drought:{DOMAIN}:2026-10"


# ── Taxonomy persistence pin (house rule after tech_churn) ─────────────────


def test_drought_candidate_survives_normalize_batch():
    tax = Taxonomy.load()
    account = Account(domain=DOMAIN, name="Acme")
    cand = drought_candidates(
        rows(months_ago(TODAY, 18)), domain=DOMAIN, today=TODAY.isoformat()
    )[0]
    valid, rej = normalize_batch(
        [cand], account=account, source="sec_formd", taxonomy=tax, now=TODAY.isoformat()
    )
    assert not rej
    sig = valid[0]
    assert sig.signal_type == "funding_drought"
    assert sig.category == "financial"
    assert sig.polarity == "negative"
    assert sig.catalyst == "secondary"
    assert sig.domain == DOMAIN
    assert sig.observed_at == TODAY.isoformat()
    assert sig.evidence_data["months_silent"] == 18


# ── Runner wiring: the per-account pass derives + persists the drought ──────


class _FakeFetch:
    def __init__(self, by_url: dict):
        self.by_url = by_url

    def get(self, task, *, etag=None, last_modified=None):
        body = self.by_url[task.url]
        doc = Document(
            doc_id="d", source=task.source, url=task.url,
            domain=task.domain, body=bytes(body), status=200,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _PlainStub(SourceAdapter):
    """Adapter-shaped stub with no harvests: drives the per-account pass so
    the funding-drought finalize block runs without real HTTP."""

    key = "techstack"
    tier = "http"
    cadence_hours = 168

    def plan(self, account, cursor):
        return [FetchTask(source=self.key, url=f"https://{account.domain}/", domain=account.domain)]

    def parse(self, doc, account, task_meta):
        return []


def _harness(tmp_path, seeded_months_silent: int | None):
    """Runner over a db optionally pre-seeded with one funding_form_d signal.

    Seeding computes observed_at relative to the real clock because the
    runner itself runs on the real clock (19 months sits far enough from the
    18/24 window edges that a midnight rollover cannot flip an assertion).
    """
    db = Database(tmp_path / "s.db")
    tax = Taxonomy.load()
    store = RawStore(db, tmp_path / "raw")
    cfg = Config()
    cfg.http.max_workers = 2
    cfg.http.respect_robots = False
    account = Account(domain=DOMAIN, name="Acme")
    if seeded_months_silent is not None:
        cand = SignalCandidate(
            signal_type="funding_form_d",
            observed_at=months_ago((datetime.now(timezone.utc).date()), seeded_months_silent).isoformat(),
            natural_key="d-2025-02",
            title="Acme Form D",
            confidence=0.9,
        )
        valid, rej = normalize_batch(
            [cand], account=account, source="sec_formd", taxonomy=tax, now=(datetime.now(timezone.utc).date()).isoformat()
        )
        assert valid and not rej
        SignalStore(db, tax).upsert_many(valid)
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    fetcher = _FakeFetch({f"https://{DOMAIN}/": b"<html><body>ok</body></html>"})
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store, fetcher, SignalStore(db, tax), tax, ctx
    )
    return runner, db, ctx


def test_runner_persists_funding_drought_for_silent_account(tmp_path):
    runner, db, ctx = _harness(tmp_path, seeded_months_silent=19)
    adapters = [_PlainStub()]
    stats1 = runner.run(adapters, [Account(domain=DOMAIN, name="Acme")], force=True)
    rows_ = db.query("SELECT * FROM signals WHERE signal_type='funding_drought'")
    assert len(rows_) == 1
    row = rows_[0]
    assert row["domain"] == DOMAIN
    assert row["source"] == "sec_formd"
    assert row["polarity"] == "negative"
    # natural_key is hashed into signal_id (sha256(domain|type|natural_key)).
    month_key = f"drought:{DOMAIN}:{(datetime.now(timezone.utc).date()).isoformat()[:7]}"
    assert row["signal_id"] == make_signal_id(DOMAIN, "funding_drought", month_key)
    ev = json.loads(row["evidence_data"])
    assert ev["months_silent"] == 19
    assert stats1.signals_new == 1
    # Re-run: the monthly natural key dedupes -> zero new signals.
    stats2 = runner.run(adapters, [Account(domain=DOMAIN, name="Acme")], force=True)
    assert stats2.signals_new == 0
    assert len(db.query("SELECT * FROM signals WHERE signal_type='funding_drought'")) == 1
    ctx.__exit__(None, None, None)


def test_runner_stays_silent_when_raise_is_fresh(tmp_path):
    runner, db, ctx = _harness(tmp_path, seeded_months_silent=3)
    stats = runner.run([_PlainStub()], [Account(domain=DOMAIN, name="Acme")], force=True)
    assert db.query("SELECT * FROM signals WHERE signal_type='funding_drought'") == []
    assert stats.signals_new == 0
    ctx.__exit__(None, None, None)

"""Task 8 (roadmap 2026-09-04): GitHub momentum from already-fetched repo JSON.

``parse_repos`` already returns the full repo JSON list but only ``pushed_at``
(stagnation) and releases were used. This pins:

- the pure ``repo_delta`` helper (new_repo / star_surge / archived tuples
  against the previous cycle's per-repo stats, thresholds: new = created
  within 90 days of the cycle date or no parseable created_at; surge =
  >= max(25% of prev, 10 absolute) star growth; archived = truthy now and
  not archived before);
- the duck-typed ``harvest_repos`` runner hook on the community_github
  adapter (kind == "repos" docs ONLY, harvest_tech/harvest_reviews
  precedent) and the runner's load -> diff -> save -> persist block with
  ``exclusive_lock`` over a stats file keyed by domain, storing per-repo
  dicts — mirror of the marketplace review-trend structure;
- monthly natural-key re-affirmation (``github:{domain}:{repo}:{kind}:{iso_
  month}``) and dedupe within the month;
- ``github_momentum`` survives normalize_batch (taxonomy pre-registered).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from src.core.models import Account
from src.signals.normalize import make_signal_id, normalize_batch
from src.signals.taxonomy import Taxonomy
from src.sources.base import FetchTask, SignalCandidate, SourceAdapter
from src.sources.community.github import repo_delta

TODAY = date(2026, 9, 5)


def _repo(name: str, stars: int = 0, created: str | None = None, archived: bool = False) -> dict:
    d = {
        "full_name": name,
        "stargazers_count": stars,
        "archived": archived,
        "pushed_at": "2026-08-01T00:00:00Z",
    }
    if created is not None:
        d["created_at"] = created
    return d


def _prev(stars: int, archived: bool = False) -> dict:
    return {"stars": stars, "archived": archived, "seen_at": "2026-08-01"}


# ── pure repo_delta ──────────────────────────────────────────────────────────


def test_repo_delta_new_repo_recently_created():
    out = repo_delta({}, [_repo("acme/new-app", stars=3, created="2026-08-20T10:00:00Z")], today=TODAY)
    assert [k for k, _ in out] == ["new_repo"]
    _kind, ev = out[0]
    assert ev["repo"] == "acme/new-app"
    assert ev["stars"] == 3
    assert ev["stars_prev"] is None
    assert ev["detail"]


def test_repo_delta_new_repo_90_day_boundary():
    # Exactly 90 days before 2026-09-05 -> still a new bet; 91 -> not.
    fresh = repo_delta({}, [_repo("acme/a", stars=1, created="2026-06-07T00:00:00Z")], today=TODAY)
    assert [k for k, _ in fresh] == ["new_repo"]
    stale = repo_delta({}, [_repo("acme/a", stars=1, created="2026-06-06T00:00:00Z")], today=TODAY)
    assert stale == []


def test_repo_delta_new_repo_without_parseable_created_at_falls_back():
    """A repo with no parseable created_at is new to us -> emit new_repo."""
    out = repo_delta({}, [_repo("acme/mystery")], today=TODAY)
    assert [k for k, _ in out] == ["new_repo"]
    garbage = repo_delta({}, [_repo("acme/mystery", created="not-a-date")], today=TODAY)
    assert [k for k, _ in garbage] == ["new_repo"]


def test_repo_delta_no_prev_stats_only_new_repo_cases():
    """With no previous stats nothing but new_repo can fire: star_surge needs
    a prior row and archived needs a prior row that was not archived. Old
    repos seen for the first time are NOT momentum (not a fresh bet)."""
    repos = [
        _repo("acme/fresh", stars=5, created="2026-08-01T00:00:00Z"),
        _repo("acme/old", stars=500, created="2019-01-01T00:00:00Z"),
        _repo("acme/mystery", stars=10),
        _repo("acme/old-archived", stars=50, created="2019-01-01T00:00:00Z", archived=True),
    ]
    kinds = [(k, ev["repo"]) for k, ev in repo_delta({}, repos, today=TODAY)]
    assert ("new_repo", "acme/fresh") in kinds
    assert ("new_repo", "acme/mystery") in kinds
    assert all(repo not in {"acme/old", "acme/old-archived"} for _, repo in kinds)


def test_repo_delta_star_surge_percentage_boundary():
    prev = {"acme/tool": _prev(100)}
    # +24% of 100 is below the 25% threshold -> silent; +25% -> fires.
    assert repo_delta(prev, [_repo("acme/tool", stars=124)], today=TODAY) == []
    surge = repo_delta(prev, [_repo("acme/tool", stars=125)], today=TODAY)
    assert [k for k, _ in surge] == ["star_surge"]
    _kind, ev = surge[0]
    assert ev["repo"] == "acme/tool"
    assert ev["stars"] == 125
    assert ev["stars_prev"] == 100
    assert ev["detail"]


def test_repo_delta_star_surge_absolute_floor():
    """The threshold is max(25% of prev, 10 absolute): 20 stars needs only
    +10 (the floor), not +5."""
    prev = {"acme/tool": _prev(20)}
    assert repo_delta(prev, [_repo("acme/tool", stars=29)], today=TODAY) == []
    assert [k for k, _ in repo_delta(prev, [_repo("acme/tool", stars=30)], today=TODAY)] == ["star_surge"]


def test_repo_delta_star_drop_and_flat_are_silent():
    prev = {"acme/tool": _prev(100)}
    assert repo_delta(prev, [_repo("acme/tool", stars=80)], today=TODAY) == []
    assert repo_delta(prev, [_repo("acme/tool", stars=100)], today=TODAY) == []


def test_repo_delta_archived_transitions():
    prev = {"acme/tool": _prev(80, archived=False)}
    out = repo_delta(prev, [_repo("acme/tool", stars=80, archived=True)], today=TODAY)
    assert [k for k, _ in out] == ["archived"]
    assert out[0][1]["repo"] == "acme/tool"
    # Already archived before -> no repeat; not archived now -> nothing.
    already = {"acme/tool": _prev(80, archived=True)}
    assert repo_delta(already, [_repo("acme/tool", stars=80, archived=True)], today=TODAY) == []
    assert repo_delta(already, [_repo("acme/tool", stars=80)], today=TODAY) == []


def test_repo_delta_skips_entries_without_full_name():
    out = repo_delta({}, [{"stargazers_count": 5, "created_at": "2026-08-01"}, _repo("acme/ok", stars=1, created="2026-08-01")], today=TODAY)
    assert [(k, ev["repo"]) for k, ev in out] == [("new_repo", "acme/ok")]


# ── stats state file (mirrors marketplace/trend.py's load/save helpers) ─────


def test_stats_roundtrip_missing_or_corrupt(tmp_path):
    from src.sources.community.stats import load_stats, save_stats

    path = tmp_path / "stats.json"
    state = {
        "acme.com": {
            "acme/tool": {"stars": 100, "archived": False, "seen_at": "2026-09-05"},
            "acme/beta": {"stars": 3, "archived": True, "seen_at": "2026-09-05"},
        }
    }
    save_stats(state, path)
    assert load_stats(path) == state
    # Missing or corrupt state is tolerated as empty (first observed cycle).
    assert load_stats(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_stats(bad) == {}


def test_stats_save_is_atomic_no_tmp_left_behind(tmp_path):
    from src.sources.community.stats import DEFAULT_STATS_PATH, load_stats, save_stats

    # The default state home for the github momentum signal.
    assert DEFAULT_STATS_PATH == "data/github/stats.json"
    # Parent dirs are created on demand; the write lands via os.replace so
    # no .tmp sibling survives (the empty_log atomic-write precedent).
    path = tmp_path / "nested" / "github" / "stats.json"
    save_stats({"acme.com": {}}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"acme.com": {}}
    assert not (tmp_path / "nested" / "github" / "stats.json.tmp").exists()
    # A second save fully REPLACES the state (os.replace semantics, no merge
    # with stale content from the previous cycle).
    save_stats({"other.com": {}}, path)
    assert load_stats(path) == {"other.com": {}}


# ── persistence (taxonomy survival, the tech_churn house rule) ──────────────


def test_github_momentum_persists_through_normalize_batch():
    out = repo_delta(
        {"acme/tool": _prev(100)},
        [_repo("acme/tool", stars=130)],
        today=TODAY,
    )
    month = TODAY.isoformat()[:7]
    cands = [
        SignalCandidate(
            signal_type="github_momentum",
            observed_at=TODAY.isoformat(),
            natural_key=f"github:acme.com:{ev['repo']}:{kind}:{month}",
            title=ev["repo"],
            confidence=0.6,
            evidence_data={"momentum_kind": kind, **ev},
        )
        for kind, ev in out
    ]
    assert len(cands) == 1
    valid, rejected = normalize_batch(
        cands,
        account=Account(domain="acme.com", name="Acme"),
        source="community_github",
        taxonomy=Taxonomy.load(),
        now="2026-09-05T00:00:00+00:00",
    )
    assert len(valid) == 1
    assert rejected == []
    sig = valid[0]
    assert sig.signal_type == "github_momentum"
    assert sig.source == "community_github"
    # The pre-registered evidence template needs exactly these keys.
    assert sig.evidence_data["momentum_kind"] == "star_surge"
    assert sig.evidence_data["detail"]


# ── runner wiring: harvest_repos hook + stats load/diff/save/persist ────────


class _GithubStub(SourceAdapter):
    """community_github-shaped stub: plan() goes straight to a kind="repos"
    task (the real adapter reaches it via follow_tasks on the org doc) and
    harvest_repos yields the repo dicts exactly like the real hook."""

    key = "community_github"
    tier = "http"
    cadence_hours = 48

    def __init__(self, repos: list[dict]):
        self.repos = repos

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url="https://api.github.com/orgs/acme/repos?per_page=100",
                domain=account.domain,
                meta={"kind": "repos", "org": "acme"},
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_repos(self, doc, account, task_meta):
        if (task_meta or {}).get("kind") != "repos" or not doc.body:
            return []
        return self.repos


class _Fetch:
    def __init__(self, store):
        self.store = store

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        doc = self.store.put(
            source=task.source,
            url=task.url,
            body=b"repos",
            content_type="application/json",
            status=200,
            domain=task.domain,
        )
        return FetchResult(True, 200, doc, False, None, 1)


def _runner(tmp_path, monkeypatch, fetch_factory=None):
    import src.sources.community.stats as gh_stats
    from src.core.config import Config
    from src.core.db import Database
    from src.core.rawstore import RawStore
    from src.core.runlog import RunContext
    from src.identity.registry import AccountRegistry
    from src.pipeline.runner import CollectorRunner
    from src.signals.store import SignalStore
    from src.signals.taxonomy import Taxonomy

    stats_path = tmp_path / "github_stats.json"
    real_load, real_save = gh_stats.load_stats, gh_stats.save_stats
    monkeypatch.setattr(gh_stats, "DEFAULT_STATS_PATH", str(stats_path))
    # Path-aware redirects: the github momentum block calls load_stats()/
    # save_stats(state) with the (redirected) module default, while the npm
    # momentum block (community_npm, Task C3) passes its OWN redirected path
    # explicitly — both must land in their own tmp file, never at data/.
    monkeypatch.setattr(
        gh_stats, "load_stats", lambda path=str(stats_path), **k: real_load(path)
    )
    monkeypatch.setattr(
        gh_stats, "save_stats", lambda st, path=str(stats_path), **k: real_save(st, path)
    )

    db = Database(tmp_path / "s.db")
    cfg = Config()
    cfg.http.max_workers = 1
    cfg.http.respect_robots = False
    store = RawStore(db, tmp_path / "raw")
    tax = Taxonomy.load()
    ctx = RunContext(db, "collect")
    ctx.__enter__()
    runner = CollectorRunner(
        cfg, db, AccountRegistry(db), store,
        fetch_factory(store) if fetch_factory else _Fetch(store),
        SignalStore(db, tax), tax, ctx,
    )
    return runner, db, ctx, stats_path


def test_runner_persists_github_momentum_and_writes_stats(tmp_path, monkeypatch):
    repos = [
        _repo("acme/tool", stars=100, created="2019-01-01T00:00:00Z"),
        _repo("acme/beta", stars=3, created="2026-08-20T10:00:00Z"),
    ]
    stub = _GithubStub(repos)
    runner, db, ctx, stats_path = _runner(tmp_path, monkeypatch)
    month = (datetime.now(UTC).date()).isoformat()[:7]
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        # Cycle 1: no prior stats -> conservative; only the recently created
        # repo fires new_repo (the old repo is not a fresh bet).
        runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
        assert len(rows) == 1
        row = rows[0]
        assert row["domain"] == "acme.com"
        assert row["source"] == "community_github"
        assert row["title"] == "acme/beta"
        assert json.loads(row["evidence_data"])["momentum_kind"] == "new_repo"
        assert row["signal_id"] == make_signal_id(
            "acme.com", "github_momentum", f"github:acme.com:acme/beta:new_repo:{month}"
        )
        # The stats file was written under the (redirected) tmp path, keyed
        # by domain with per-repo dicts — never at data/.
        assert stats_path.exists()
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        assert set(stats) == {"acme.com"}
        assert stats["acme.com"]["acme/tool"] == {
            "stars": 100,
            "archived": False,
            "seen_at": (datetime.now(UTC).date()).isoformat(),
            # Topic momentum (2026-10-30): the snapshot now persists casefolded
            # topics so repo_delta can diff them next cycle (empty here — the
            # stub repos carry none).
            "topics": [],
        }
        assert stats["acme.com"]["acme/beta"]["stars"] == 3

        # Cycle 2: +30% on acme/tool crosses the 25% threshold -> star_surge.
        repos[0]["stargazers_count"] = 130
        stats2 = runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
        assert len(rows) == 2
        kinds = {json.loads(r["evidence_data"])["momentum_kind"] for r in rows}
        assert kinds == {"new_repo", "star_surge"}
        surge = next(r for r in rows if json.loads(r["evidence_data"])["momentum_kind"] == "star_surge")
        assert json.loads(surge["evidence_data"])["repo"] == "acme/tool"
        assert json.loads(surge["evidence_data"])["stars_prev"] == 100
        assert stats2.by_source["community_github"]["signals_new"] == 1

        # Cycle 3: unchanged snapshot -> the monthly natural keys dedupe.
        stats3 = runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
        assert len(rows) == 2
        assert stats3.signals_new == 0
    finally:
        ctx.__exit__(None, None, None)


def test_community_github_harvest_repos_is_repos_docs_only():
    """The real adapter's hook returns parsed repos for kind == "repos" docs
    ONLY — org/releases docs (HTML/other JSON) yield []."""
    from src.sources.community.collector import CommunityGithubSource

    src = CommunityGithubSource()
    acct = Account(domain="acme.com")
    repos_body = json.dumps([{"full_name": "acme/tool", "stargazers_count": 1}]).encode("utf-8")
    doc = type("D", (), {"body": repos_body})()
    assert src.harvest_repos(doc, acct, {"kind": "repos"}) == [
        {"full_name": "acme/tool", "stargazers_count": 1}
    ]
    assert src.harvest_repos(doc, acct, {"kind": "org"}) == []
    assert src.harvest_repos(doc, acct, {"kind": "releases"}) == []
    assert src.harvest_repos(doc, acct, {}) == []
    assert src.harvest_repos(type("D", (), {"body": b""})(), acct, {"kind": "repos"}) == []


def test_two_new_topics_persist_two_signals(tmp_path, monkeypatch):
    """A repo gaining 2+ topics in ONE cycle emits one new_topic event per
    topic — the natural key MUST carry the topic, or both candidates collide
    on `github:{domain}:{repo}:new_topic:{month}` and SignalStore.upsert
    merges them into one row while the snapshot already records every topic,
    so the dropped topic never re-fires."""
    repos = [_repo("acme/tool", stars=100, created="2019-01-01T00:00:00Z")]
    stub = _GithubStub(repos)
    runner, db, ctx, _stats_path = _runner(tmp_path, monkeypatch)
    month = (datetime.now(UTC).date()).isoformat()[:7]
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        # Cycle 1: baseline snapshot (no topics) — old repo, no new_repo bet.
        runner.run([stub], accounts, force=True)
        assert db.query("SELECT * FROM signals WHERE signal_type='github_momentum'") == []
        # Cycle 2: the repo gains two topics in the same cycle.
        repos[0]["topics"] = ["ai", "mcp"]
        runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
        assert len(rows) == 2, f"topic key collision dropped a signal: {rows}"
        keys = {r["signal_id"] for r in rows}
        assert keys == {
            make_signal_id("acme.com", "github_momentum", f"github:acme.com:acme/tool:new_topic:ai:{month}"),
            make_signal_id("acme.com", "github_momentum", f"github:acme.com:acme/tool:new_topic:mcp:{month}"),
        }
        evidence = [json.loads(r["evidence_data"]) for r in rows]
        assert {e["topic"] for e in evidence} == {"ai", "mcp"}
        assert all(e["momentum_kind"] == "new_topic" for e in evidence)
    finally:
        ctx.__exit__(None, None, None)


# ── npm momentum wiring (community_npm, Task C3 mirror of the block above) ──


class _NpmStub(SourceAdapter):
    """community_npm-shaped stub: plan() emits the registry search task (the
    real adapter's shape) and harvest_packages yields the owned package dicts
    exactly like the real hook (NpmSource.harvest_packages)."""

    key = "community_npm"
    tier = "http"
    cadence_hours = 72

    def __init__(self, pkgs: list[dict]):
        self.pkgs = pkgs

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url="https://registry.npmjs.org/-/v1/search?text=acme&size=20",
                domain=account.domain,
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def harvest_packages(self, doc, account, task_meta):
        if not doc.body:
            return []
        return self.pkgs


def test_runner_persists_npm_momentum_and_writes_stats(tmp_path, monkeypatch):
    """community_npm mirror of the github momentum wiring above: the runner
    harvests owned packages via the duck-typed harvest_packages hook, diffs
    them with npm_delta under an exclusive_lock over the npm stats file, and
    persists a product_launch for a genuinely-new package (last publish
    within NEW_PACKAGE_WINDOW_DAYS) with an npm: monthly natural key."""
    import src.sources.community.npm as npm_mod

    now_date = datetime.now(UTC).date()
    published = (now_date - timedelta(days=10)).isoformat()
    pkgs = [
        {
            "name": "@acme/sdk",
            "version": "1.0.0",
            "date": f"{published}T00:00:00.000Z",
            "description": "The Acme SDK",
            "downloads_monthly": 5,
            "downloads_weekly": 1,
            "repository": "git+https://github.com/acmecorp/sdk.git",
            "publisher": "acme",
        }
    ]
    stub = _NpmStub(pkgs)
    npm_stats_path = tmp_path / "npm_stats.json"
    monkeypatch.setattr(npm_mod, "DEFAULT_STATS_PATH", str(npm_stats_path))
    runner, db, ctx, _gh_stats_path = _runner(tmp_path, monkeypatch)
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        stats = runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='product_launch'")
        assert len(rows) == 1
        row = rows[0]
        assert row["domain"] == "acme.com"
        assert row["source"] == "community_npm"
        assert row["title"] == "@acme/sdk"
        evidence = json.loads(row["evidence_data"])
        assert evidence["via"] == "npm"
        assert evidence["published"] == published
        # product_launch's evidence template reads {product} — without the
        # key the rendered line degrades to "Launched  (…)".
        assert evidence["product"] == "@acme/sdk"
        assert row["signal_id"] == make_signal_id(
            "acme.com",
            "product_launch",
            f"npm:acme.com:@acme/sdk:new_package:{now_date.isoformat()[:7]}",
        )
        assert stats.by_source["community_npm"]["signals_new"] == 1
        # The stats file was written under the (redirected) tmp path, keyed
        # by domain with per-package dicts — never at data/.
        assert npm_stats_path.exists()
        saved = json.loads(npm_stats_path.read_text(encoding="utf-8"))
        assert set(saved) == {"acme.com"}
        assert saved["acme.com"]["@acme/sdk"] == {
            "version": "1.0.0",
            "downloads_monthly": 5,
            "seen_at": now_date.isoformat(),
        }
    finally:
        ctx.__exit__(None, None, None)


def test_runner_persists_npm_downloads_surge_momentum_kind(tmp_path, monkeypatch):
    """The npm downloads_surge rides github_momentum, whose evidence template
    reads ``{momentum_kind}: {detail}`` — the npm evidence must carry a
    namespaced momentum_kind (npm_downloads_surge), mirroring how the github
    momentum block injects momentum_kind, or the rendered line degrades to
    ": monthly downloads …"."""
    import src.sources.community.npm as npm_mod

    now_date = datetime.now(UTC).date()
    published = (now_date - timedelta(days=10)).isoformat()
    pkgs = [{
        "name": "@acme/sdk", "version": "1.0.0",
        "date": f"{published}T00:00:00.000Z", "downloads_monthly": 200,
        "repository": "git+https://github.com/acmecorp/sdk.git",
    }]
    stub = _NpmStub(pkgs)
    monkeypatch.setattr(npm_mod, "DEFAULT_STATS_PATH", str(tmp_path / "npm_stats.json"))
    runner, db, ctx, _gh = _runner(tmp_path, monkeypatch)
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        runner.run([stub], accounts, force=True)  # cycle 1: new_package baseline
        pkgs[0]["downloads_monthly"] = 400  # +200 >= max(25% of 200, 50) -> surge
        runner.run([stub], accounts, force=True)
        rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
        assert len(rows) == 1
        evidence = json.loads(rows[0]["evidence_data"])
        assert evidence["momentum_kind"] == "npm_downloads_surge"
        assert evidence["via"] == "npm"
        assert evidence["downloads"] == 400 and evidence["downloads_prev"] == 200
        assert evidence["detail"]
    finally:
        ctx.__exit__(None, None, None)


# ── recoverable org probes: runner softening (2026-10-31) ────────────────────


class _StatusFetch:
    """Fetcher stub mirroring src/core/http.py's failure seam: ok=False,
    doc=None, error="HTTP <code>" for fail_urls (default 404) or a per-URL
    code via fail_status (which wins); 200 + body otherwise, with per-URL
    bodies via ``bodies`` (default body otherwise)."""

    def __init__(
        self,
        store,
        fail_urls: set[str] | None = None,
        body: bytes = b'{"items": []}',
        fail_status: dict[str, int] | None = None,
        bodies: dict[str, bytes] | None = None,
    ):
        self.store = store
        self.fail_urls = fail_urls or set()
        self.body = body
        self.fail_status = fail_status or {}
        self.bodies = bodies or {}

    def get(self, task, *, etag=None, last_modified=None):
        from src.core.http import FetchResult

        if task.url in self.fail_status:
            code = self.fail_status[task.url]
            return FetchResult(False, code, None, False, f"HTTP {code}", 1)
        if task.url in self.fail_urls:
            return FetchResult(False, 404, None, False, "HTTP 404", 1)
        doc = self.store.put(
            source=task.source,
            url=task.url,
            body=self.bodies.get(task.url, self.body),
            content_type="application/json",
            status=200,
            domain=task.domain,
        )
        return FetchResult(True, 200, doc, False, None, 1)


class _RecoverPairStub(SourceAdapter):
    """community_github-shaped stub for the live 404+recovery pair
    (astranis.com / alpha-sense.com, 2026-10-30): plan() emits a recoverable
    org probe that 404s next to a sibling search task that 200s; the search
    doc's follow_tasks emits one extra task whose parse yields a candidate."""

    key = "community_github"
    tier = "http"
    cadence_hours = 48

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url="https://api.github.com/orgs/acme",
                domain=account.domain,
                meta={"kind": "org", "recoverable": True},
            ),
            FetchTask(
                source=self.key,
                url="https://api.github.com/search/users?q=acme%20in%3Alogin&per_page=5",
                domain=account.domain,
                meta={"kind": "search_users"},
            ),
        ]

    def parse(self, doc, account, task_meta):
        if (task_meta or {}).get("kind") != "recovered":
            return []
        return [
            SignalCandidate(
                signal_type="github_momentum",
                observed_at=task_meta["today"],
                natural_key=f"github:{account.domain}:recovered:test",
                title="recovered sibling",
                confidence=0.5,
                evidence_data={"momentum_kind": "test"},
            )
        ]

    def follow_tasks(self, doc, account, task_meta):
        if (task_meta or {}).get("kind") == "search_users":
            return [
                FetchTask(
                    source=self.key,
                    url="https://api.github.com/orgs/acme/repos?per_page=100",
                    domain=account.domain,
                    meta={"kind": "recovered", "org": "acme"},
                )
            ]
        return []


def test_recoverable_404_lets_pair_live_and_sibling_follow_persists(tmp_path, monkeypatch):
    """A failed fetch on a task flagged meta["recoverable"] (community_github
    org probe 404: no org at this guess) is skipped NON-fatally: no parse, no
    follow from it — but the pair lives, so the sibling search task still
    processes and its follow task's candidate PERSISTS. The clean 404 must not
    count as a source failure (no stats.failed, no fail_count/backoff poison)."""
    stub = _RecoverPairStub()
    runner, db, ctx, _stats_path = _runner(
        tmp_path, monkeypatch,
        fetch_factory=lambda store: _StatusFetch(
            store, fail_urls={"https://api.github.com/orgs/acme"},
        ),
    )
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        stats = runner.run([stub], accounts, force=True)
    finally:
        ctx.__exit__(None, None, None)
    # No pair failure: the recoverable 404 did not raise out of _run_pair.
    assert stats.failed == 0
    assert stats.by_source["community_github"]["failed"] == 0
    # The sibling search task processed and its follow task's candidate
    # persisted end-to-end.
    assert stats.signals_new == 1
    rows = db.query("SELECT * FROM signals WHERE signal_type='github_momentum'")
    assert len(rows) == 1
    assert rows[0]["domain"] == "acme.com"
    assert rows[0]["source"] == "community_github"
    assert rows[0]["title"] == "recovered sibling"
    # Pair recorded success: ledger row ran_data, cursor fail_count stays 0.
    outcome = next(o for o in stats.outcomes if o["source"] == "community_github")
    assert outcome["status"] == "ran_data"
    cur = db.one("SELECT * FROM source_cursors WHERE source='community_github'")
    assert cur["fail_count"] == 0
    assert cur["last_error"] is None


class _NoRecoverStub(SourceAdapter):
    """The same 404 probe WITHOUT the recoverable flag: the runner softening
    is flag-scoped, so this must still raise out of _run_pair."""

    key = "community_github"
    tier = "http"
    cadence_hours = 48

    def plan(self, account, cursor):
        return [
            FetchTask(
                source=self.key,
                url="https://api.github.com/orgs/acme",
                domain=account.domain,
                meta={"kind": "org"},
            )
        ]

    def parse(self, doc, account, task_meta):
        return []

    def follow_tasks(self, doc, account, task_meta):
        return []


def test_non_recoverable_404_still_raises(tmp_path, monkeypatch):
    """Pins the flag-scoping of the runner softening: an ok=False fetch on a
    task WITHOUT meta["recoverable"] still raises RuntimeError from
    _run_pair (real failures must surface). Pins existing behavior — passes
    before AND after the softening; an unflagged-softening regression would
    turn this RED."""
    from src.pipeline.runner import RunnerStats, _now

    stub = _NoRecoverStub()
    runner, _db, ctx, _stats_path = _runner(
        tmp_path, monkeypatch,
        fetch_factory=lambda store: _StatusFetch(
            store, fail_urls={"https://api.github.com/orgs/acme"},
        ),
    )
    try:
        with pytest.raises(RuntimeError):
            runner._run_pair(
                stub,
                Account(domain="acme.com", name="Acme"),
                RunnerStats(),
                force=True,
                dry_run=False,
                max_passes=2,
                limit_per_source=None,
                now=_now(),
            )
    finally:
        ctx.__exit__(None, None, None)


def test_follow_task_404_is_expected_miss(tmp_path, monkeypatch):
    """End-to-end on the REAL CommunityGithubSource (alpha-sense.com live,
    2026-10-31): the org probe 200s, follow_tasks emits the repos/releases/
    events trio, and /orgs/{org}/releases 404s because GitHub returns 404 for
    orgs with ZERO public repos while /orgs/{org}/repos returns 200 []. For
    this source absence is DATA (the stagnation detector needs the empty case
    calm), so the releases 404 on a follow task must be skipped non-fatally:
    no raise, the pair succeeds, and whatever the repos doc parsed still
    persists (one stale repo -> one stagnation signal). The 404 must not
    poison fail_count/backoff."""
    from src.sources.community.collector import CommunityGithubSource

    org_body = json.dumps({"login": "acme"}).encode("utf-8")
    repos_body = json.dumps(
        [
            {
                "full_name": "acme/old",
                "stargazers_count": 5,
                "pushed_at": "2024-01-01T00:00:00Z",
                "created_at": "2019-01-01T00:00:00Z",
            }
        ]
    ).encode("utf-8")
    runner, db, ctx, _stats_path = _runner(
        tmp_path, monkeypatch,
        fetch_factory=lambda store: _StatusFetch(
            store,
            bodies={
                "https://api.github.com/orgs/acme": org_body,
                "https://api.github.com/orgs/acme/repos?per_page=100": repos_body,
                "https://api.github.com/orgs/acme/events?per_page=30": b"[]",
            },
            fail_status={"https://api.github.com/orgs/acme/releases?per_page=20": 404},
        ),
    )
    accounts = [Account(domain="acme.com", name="Acme")]
    try:
        stats = runner.run([CommunityGithubSource()], accounts, force=True)
    finally:
        ctx.__exit__(None, None, None)
    # No pair failure: the follow-task 404 did not raise out of _run_pair.
    assert stats.failed == 0
    assert stats.by_source["community_github"]["failed"] == 0
    # Everything else fetched: org probe + search (pass 0), repos + events
    # (pass 1); only the releases 404 was skipped.
    assert stats.by_source["community_github"]["fetched"] == 4
    # The repos sibling processed and its parsed candidate persisted
    # end-to-end (stale repo -> stagnation) despite the releases 404.
    assert stats.signals_new == 1
    rows = db.query("SELECT * FROM signals WHERE signal_type='stagnation'")
    assert len(rows) == 1
    assert rows[0]["domain"] == "acme.com"
    assert rows[0]["source"] == "community_github"
    assert rows[0]["title"] == "No GitHub pushes in 365 days"
    # Pair recorded success: cursor fail_count stays 0 (the 404 is not a
    # source failure and must not feed backoff).
    cur = db.one("SELECT * FROM source_cursors WHERE source='community_github'")
    assert cur["fail_count"] == 0
    assert cur["last_error"] is None


def test_recoverable_500_still_raises(tmp_path, monkeypatch):
    """Pins the 404-ONLY scoping of the runner softening: a flagged
    (meta["recoverable"]) task failing with a 5xx is infrastructure trouble,
    NOT an expected miss — it must still raise RuntimeError out of _run_pair
    so it stays loud and feeds fail_count/backoff. Expected-miss means 404,
    nothing else."""
    from src.pipeline.runner import RunnerStats, _now

    stub = _RecoverPairStub()
    runner, _db, ctx, _stats_path = _runner(
        tmp_path, monkeypatch,
        fetch_factory=lambda store: _StatusFetch(
            store,
            fail_status={"https://api.github.com/orgs/acme": 500},
        ),
    )
    try:
        with pytest.raises(RuntimeError):
            runner._run_pair(
                stub,
                Account(domain="acme.com", name="Acme"),
                RunnerStats(),
                force=True,
                dry_run=False,
                max_passes=2,
                limit_per_source=None,
                now=_now(),
            )
    finally:
        ctx.__exit__(None, None, None)

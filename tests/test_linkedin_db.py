from datetime import date
from pathlib import Path
from src.core.models import Contact
from src.sources.linkedin_db.jobs import linkedin_jobs_to_candidates
from src.sources.linkedin_db.people import detect_role_changes, people_to_contacts
from src.sources.linkedin_db.posts import posts_to_candidates
from src.core.config import Config
from src.sources.linkedin_db.collector import request_linkedin_deepen


TODAY = date(2026, 8, 16)


def test_contacts_and_role_rules():
    rows = [
        {"name": "Pat Lead", "linkedin_slug": "pat-lead", "job_title": "VP Sales", "experience_dates": "Mar 2026 – Present"},
        {"name": "Sam Up", "linkedin_slug": "sam-up", "job_title": "Director of Sales", "prior_title": "Account Executive"},
        {"name": "Old Boss", "linkedin_slug": "old-boss", "job_title": "CFO", "role_ended": "2026-07-01"},
    ]
    contacts = people_to_contacts(rows, "acme.com")
    assert len(contacts) == 3
    champs = {"pat-lead": {"prior_domain": "gong.io", "prior_company": "Gong"}}
    cands = detect_role_changes(contacts, rows, champs, today=TODAY)
    types = {c.signal_type for c in cands}
    assert "exec_hire" in types
    assert "champion_migration" in types
    assert "promotion" in types
    assert "exec_departure" in types
    champ = next(c for c in cands if c.signal_type == "champion_migration")
    assert champ.evidence_data["prior_company"] == "Gong"


def test_job_window_and_posts():
    jobs = [{"title": "VP Engineering", "listed_at": "2026-08-01", "job_id": "1"}]
    assert linkedin_jobs_to_candidates(jobs, "acme.com", today=TODAY)
    old = [{"title": "VP Engineering", "listed_at": "2026-01-01", "job_id": "2"}]
    assert linkedin_jobs_to_candidates(old, "acme.com", today=TODAY) == []
    posts = [
        {"author_kind": "company", "text": "Announcing Widget", "posted_at": "2026-08-01", "post_urn": "u1"},
        {"author_kind": "person", "author_slug": "jane", "text": "hello", "posted_at": "2026-08-01", "post_urn": "u2"},
        {"author_kind": "company", "text": "We're hiring sales people", "posted_at": "2026-08-01", "post_urn": "u3"},
    ]
    pc = posts_to_candidates(posts, "acme.com", [Contact(person_key="jane", linkedin_slug="jane")], today=TODAY)
    types = {c.signal_type for c in pc}
    assert "product_launch" in types
    assert "content_appearance" in types
    assert "department_expansion" in types


def test_deepen_failure_path(monkeypatch, tmp_path):
    """subprocess.run raising -> clean None (never crashes the caller).
    Requires a checkout+venv that exist so the pre-run guards pass — both are
    faked with tmp_path."""
    from src.core.config import Config
    cfg = Config()
    cfg.external_dbs.linkedin_cli_cwd = str(tmp_path / "Linkedin")
    (tmp_path / "Linkedin" / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "Linkedin" / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    called = {}

    def boom(*a, **k):
        called["x"] = True
        raise RuntimeError("nope")

    monkeypatch.setattr("src.sources.linkedin_db.collector.subprocess.run", boom)
    monkeypatch.chdir(tmp_path)
    assert request_linkedin_deepen(cfg, "acme") is None
    assert called


def test_deepen_invokes_extract_not_enrich(monkeypatch, tmp_path):
    """The scraper's `enrich` command needs prior pipeline state; `extract` is
    the single-company entry point. argv must use extract + --url."""
    calls = {}
    fake_root = tmp_path / "Linkedin"
    (fake_root / ".venv" / "Scripts").mkdir(parents=True)
    (fake_root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")

    class P:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["cwd"] = kw.get("cwd")
        return P()

    monkeypatch.setattr("src.sources.linkedin_db.collector.subprocess.run", fake_run)
    cfg = Config()
    cfg.external_dbs.linkedin_cli_cwd = str(fake_root)
    monkeypatch.chdir(tmp_path)
    request_linkedin_deepen(cfg, "acme-corp", max_people=12, timeout=60)
    argv = calls["argv"]
    assert "extract" in argv, argv
    assert "--url" in argv and "https://www.linkedin.com/company/acme-corp/" in " ".join(argv)
    assert Path(calls["cwd"]) == fake_root


def test_deepen_max_people_not_sent_to_extract(monkeypatch, tmp_path):
    """`extract` has no --max-people option (only `employees` does) — passing it
    would exit 2. The kwarg is accepted for signature stability but not sent."""
    captured = {}
    fake_root = tmp_path / "Linkedin"
    (fake_root / ".venv" / "Scripts").mkdir(parents=True)
    (fake_root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return P()

    monkeypatch.setattr("src.sources.linkedin_db.collector.subprocess.run", fake_run)
    cfg = Config()
    cfg.external_dbs.linkedin_cli_cwd = str(fake_root)
    monkeypatch.chdir(tmp_path)
    request_linkedin_deepen(cfg, "acme-corp", max_people=5)
    assert "--max-people" not in captured["argv"]


def test_deepen_missing_checkout_returns_none(caplog):
    """Absent scraper checkout -> clean None + warning, never a crash."""
    cfg = Config()
    cfg.external_dbs.linkedin_cli_cwd = "Z:/definitely/not/here"
    result = request_linkedin_deepen(cfg, "acme-corp", timeout=5)
    assert result is None


def test_deepen_cli_forwards_options(monkeypatch):
    """deepen --domain X --max-people N must forward both to request_linkedin_deepen."""
    import src.cli as cli
    seen = {}

    def fake_deepen(config, slug, **kw):
        seen.update(kw)
        seen["slug"] = slug

    monkeypatch.setattr("src.sources.linkedin_db.collector.request_linkedin_deepen", fake_deepen)
    from click.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(cli.main, ["deepen", "--domain", "acme-corp", "--max-people", "9"])
    assert res.exit_code == 0, res.output
    assert seen["slug"] == "acme-corp"
    assert seen.get("max_people") == 9

"""T8 — doctor referenced-list checks; T13 — doctor GKG credential checks.

The doctor "lists" check parses config/icp.yaml and WARNs for every
domain_in_file path that does not exist on disk (Path resolution mirrors
src/identity/lists.py load_domain_list: CWD-relative, absolute paths pass
through). Empty list files are fine — missing ones are not — and a missing or
unparseable icp.yaml must degrade to a graceful skip, never raise.

The "gkg_credentials" check (plan T13) probes the credential gate of the
identity-discovery GKG stage: backend "ekg" (default) needs google-auth (the
optional [gkg] extra) plus GOOGLE_APPLICATION_CREDENTIALS; backend "kgsearch"
needs GOOGLE_KGSEARCH_KEY. Unconfigured is a WARN naming the stage and its
no-op posture, never a FAIL — and an unknown backend value must not crash.
"""

from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import Config
from src.core.db import Database
from src.pipeline.health import doctor

# Same shape as the shipped config/icp.yaml disqualifiers block.
ICP_YAML = """\
disqualifiers:
  - id: competitor
    when: { domain_in_file: "config/lists/competitors.txt" }
    reason: "Competitor"
  - id: customer
    when: { domain_in_file: "config/lists/customers.txt" }
    reason: "Existing customer — route to CS"
  - id: do_not_contact
    when: { domain_in_file: "config/lists/dnc.txt" }
    reason: "Do-not-contact list"
"""


def _make_config(tmp_path):
    cfg = Config()
    cfg.contact_email = "ops@example.com"
    cfg.config_dir = str(tmp_path / "cfg")
    cfg.storage.raw_dir = str(tmp_path / "raw")
    cfg.storage.export_dir = str(tmp_path / "ex")
    cfg.storage.briefs_dir = str(tmp_path / "br")
    cfg.external_dbs.linkedin_db = str(tmp_path / "missing.db")
    cfg.external_dbs.repvue_db = str(tmp_path / "missing2.db")
    return cfg


def _write_icp(tmp_path, text=ICP_YAML):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "icp.yaml").write_text(text, encoding="utf-8")


def _make_lists(tmp_path, names):
    """Create empty list files under <cwd>/config/lists (CWD-relative refs)."""
    lists_dir = tmp_path / "config" / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (lists_dir / name).write_bytes(b"")
    return lists_dir


def _lists_row(rows):
    return [r for r in rows if r[0] == "lists"][0]


def test_lists_ok_when_all_referenced_files_present(tmp_path, monkeypatch):
    # References are CWD-relative exactly like load_domain_list, so pin CWD.
    monkeypatch.chdir(tmp_path)
    _write_icp(tmp_path)
    _make_lists(tmp_path, ("competitors.txt", "customers.txt", "dnc.txt"))
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)
    name, status, detail = _lists_row(rows)
    assert (name, status) == ("lists", "OK")
    assert "3" in detail


def test_lists_warn_names_missing_referenced_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_icp(tmp_path)
    # competitors.txt deliberately absent — customers/dnc created.
    _make_lists(tmp_path, ("customers.txt", "dnc.txt"))
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)
    _, status, detail = _lists_row(rows)
    assert status == "WARN"
    assert "missing referenced list file(s)" in detail
    assert "config/lists/competitors.txt" in detail


def test_lists_ok_with_empty_file_stubs(tmp_path, monkeypatch):
    """Empty is fine, missing is not — header-only stubs count as present."""
    monkeypatch.chdir(tmp_path)
    _write_icp(tmp_path)
    lists_dir = _make_lists(
        tmp_path, ("competitors.txt", "customers.txt", "dnc.txt")
    )
    for name in ("competitors.txt", "customers.txt", "dnc.txt"):
        (lists_dir / name).write_text(
            "# intentionally empty; see icp.yaml domain_in_file\n", encoding="utf-8"
        )
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)
    assert _lists_row(rows)[1] == "OK"


def test_lists_skips_gracefully_when_icp_missing_or_unparseable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "s.db")
    # No icp.yaml at all.
    rows = doctor(_make_config(tmp_path), db, check_network=False)  # must not raise
    _, status, detail = _lists_row(rows)
    assert status in {"OK", "WARN"}
    assert "skipped" in detail
    # Unparseable icp.yaml.
    _write_icp(tmp_path, "disqualifiers: [oops\n  - broken:\n    :\n")
    rows = doctor(_make_config(tmp_path), db, check_network=False)  # must not raise
    _, status, detail = _lists_row(rows)
    assert status in {"OK", "WARN"}
    assert "skipped" in detail


def test_lists_tolerates_extra_unknown_references(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extra = ICP_YAML + (
        "rules:\n"
        "  - id: partner\n"
        '    when: { domain_in_file: "config/lists/partners.txt" }\n'
        "    multiplier: 0.9\n"
    )
    _write_icp(tmp_path, extra)
    _make_lists(
        tmp_path, ("competitors.txt", "customers.txt", "dnc.txt", "partners.txt")
    )
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)  # must not raise
    _, status, detail = _lists_row(rows)
    assert status == "OK"
    assert "4" in detail


# ---------------------------------------------------------------------------
# T13 — gkg_credentials check (ekg default backend, kgsearch fallback)
# ---------------------------------------------------------------------------


def _gkg_row(rows):
    return [r for r in rows if r[0] == "gkg_credentials"][0]


def _import_blocks_google(monkeypatch):
    """Force google-auth absent regardless of whether the [gkg] extra is installed."""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "google" or name.startswith("google."):
            raise ImportError("google-auth not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_gkg_credentials_warn_when_ekg_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _import_blocks_google(monkeypatch)
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)
    name, status, detail = _gkg_row(rows)
    assert name == "gkg_credentials"
    assert status == "WARN"
    assert "GKG" in detail  # names the stage
    assert "no-ops" in detail  # states the no-op posture
    assert "GOOGLE_APPLICATION_CREDENTIALS" in detail


def test_gkg_credentials_ok_when_ekg_creds_set(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "sa.json"))
    monkeypatch.delenv("GKG_PROJECT_ID", raising=False)
    # Deterministic google-auth presence: inject stub modules (reverted by
    # monkeypatch) so the check's import succeeds in any environment.
    google_mod = types.ModuleType("google")
    auth_mod = types.ModuleType("google.auth")
    google_mod.auth = auth_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.auth", auth_mod)
    db = Database(tmp_path / "s.db")
    rows = doctor(_make_config(tmp_path), db, check_network=False)
    _, status, detail = _gkg_row(rows)
    assert status == "OK"
    assert "GOOGLE_APPLICATION_CREDENTIALS" in detail
    # Honest hint: EKG also needs the project id at call time.
    assert "GKG_PROJECT_ID unset" in detail


def test_gkg_credentials_warn_when_kgsearch_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_KGSEARCH_KEY", raising=False)
    cfg = _make_config(tmp_path)
    cfg.gkg_backend = "kgsearch"
    db = Database(tmp_path / "s.db")
    rows = doctor(cfg, db, check_network=False)
    _, status, detail = _gkg_row(rows)
    assert status == "WARN"
    assert "kgsearch" in detail
    assert "GOOGLE_KGSEARCH_KEY" in detail
    assert "no-ops" in detail


def test_gkg_credentials_unknown_backend_does_not_crash(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    cfg.gkg_backend = "bogus"
    db = Database(tmp_path / "s.db")
    rows = doctor(cfg, db, check_network=False)  # must not raise
    name, status, detail = _gkg_row(rows)
    assert name == "gkg_credentials"
    assert status == "WARN"
    assert "bogus" in detail

from click.testing import CliRunner
import pytest

from scripts import pull_fixture, recon_ats, recon_ats_boards, recon_browser, recon_http


@pytest.mark.parametrize("mod", [recon_http, recon_browser, recon_ats, recon_ats_boards, pull_fixture])
def test_help_exits_0(mod):
    with pytest.raises(SystemExit) as ei:
        mod.main(["--help"])
    assert ei.value.code == 0


def test_slug_and_pull(tmp_path):
    from src.core.db import Database
    from src.core.rawstore import RawStore

    slug = recon_http.slugify_url("https://example.com/path/to?x=1")
    assert "example.com" in slug and "/" not in slug
    db = Database(tmp_path / "s.db")
    store = RawStore(db, tmp_path / "raw")
    doc = store.put(source="t", url="https://x.test/a", body=b'{"ok": true}', content_type="application/json", status=200)
    dest = tmp_path / "out.json"
    rc = pull_fixture.main(["--doc", doc.doc_id, "--dest", str(dest), "--db", str(tmp_path / "s.db"), "--raw", str(tmp_path / "raw")])
    assert rc == 0
    assert b"ok" in dest.read_bytes()

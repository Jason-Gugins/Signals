from pathlib import Path
from src.sources.crtsh.subdomains import infer_from_subdomains, parse_crtsh


def test_parse_crtsh():
    names = parse_crtsh((Path("tests/fixtures/crtsh/crtsh.json")).read_bytes())
    assert "www.acme.com" in names
    assert "acme.com" in names
    assert "status.acme.com" in names
    assert len([n for n in names if n == "status.acme.com"]) == 1
    assert parse_crtsh(b"") == []
    hints = infer_from_subdomains(names, {})
    assert any(h.vendor == "statuspage" for h in hints)
    assert all(h.confidence == 0.45 for h in hints)

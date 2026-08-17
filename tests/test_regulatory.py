from datetime import date
from pathlib import Path
import yaml
from src.core.models import Account
from src.sources.regulatory.federal_register import fr_query_url, fr_to_candidates, match_watches, parse_fr_documents

WATCHES = yaml.safe_load(Path("config/regulations.yaml").read_text(encoding="utf-8"))["watches"]


def test_url_builder():
    url = fr_query_url(["consumer privacy"], "2026-01-01")
    assert "documents.json" in url
    assert "consumer+privacy" in url or "consumer%20privacy" in url


def test_parse_and_watch_and_industry():
    docs = parse_fr_documents((Path("tests/fixtures/regulatory/fr_documents.json")).read_bytes())
    assert docs[0]["document_number"] == "2026-12345"
    ids = match_watches(docs[0], WATCHES)
    assert "privacy" in ids
    software = Account(domain="acme.com", name="Acme", industry="Software")
    cands = fr_to_candidates(docs[0], ids, software, today=date(2026, 8, 16), watches=WATCHES)
    assert cands and cands[0].signal_type == "regulation_applicable"
    other = Account(domain="mine.com", name="Mine", industry="Mining")
    assert fr_to_candidates(docs[0], ids, other, today=date(2026, 8, 16), watches=WATCHES) == []


def test_no_watch_nothing():
    doc = {"title": "Unrelated wheat tariff", "abstract": "grain"}
    assert match_watches(doc, WATCHES) == []

from pathlib import Path

import yaml

from src.core.db import Database
from src.core.models import Account, Document
from src.identity.registry import AccountRegistry
from src.sources.regulatory.collector import FederalRegisterSource
from src.sources.warn.source import WarnNoticesSource


def test_fr_parse_emits_for_software_account():
    watches = yaml.safe_load(Path("config/regulations.yaml").read_text(encoding="utf-8"))["watches"]
    body = Path("tests/fixtures/regulatory/fr_documents.json").read_bytes()
    src = FederalRegisterSource()
    acct = Account(domain="acme.com", name="Acme", industry="Software")
    cands = src.parse(
        Document(doc_id="fr", source="federal_register", body=body),
        acct,
        {"today": "2026-08-16", "watches": watches},
    )
    assert any(c.signal_type == "regulation_applicable" for c in cands)


def test_warn_parse_sets_domain_override_only_when_unique(tmp_path):
    db = Database(tmp_path / "s.db")
    AccountRegistry(db).upsert(Account(domain="acme.com", name="Acme Corp."))
    src = WarnNoticesSource()
    html = Path("tests/fixtures/warn/ny.html").read_bytes()
    cands = src.parse(
        Document(doc_id="w", source="warn_notices", body=html),
        Account(domain="unused.com"),
        {"today": "2026-08-16", "registry": AccountRegistry(db), "state": "NY"},
    )
    assert cands and cands[0].domain_override == "acme.com"

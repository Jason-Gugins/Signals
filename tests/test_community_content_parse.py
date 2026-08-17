from datetime import date
from pathlib import Path

from src.core.models import Account, Document
from src.sources.community.collector import CommunityGithubSource, CommunityHnSource
from src.sources.content.collector import ContentItunesSource


def test_hn_github_itunes_parse():
    acct = Account(domain="acme.com", name="Acme")
    meta = {"today": "2026-08-16"}
    hn = CommunityHnSource().parse(
        Document(doc_id="h", source="community_hn", body=Path("tests/fixtures/community/hn.json").read_bytes()),
        acct,
        meta,
    )
    assert any(c.signal_type == "intent_3rd_topic" for c in hn)
    gh = CommunityGithubSource()
    org_doc = Document(doc_id="g", source="community_github", body=Path("tests/fixtures/community/gh_org.json").read_bytes())
    assert gh.parse(org_doc, acct, {**meta, "kind": "org"}) == []
    follows = gh.follow_tasks(org_doc, acct, {**meta, "kind": "org"})
    assert any("/repos" in t.url for t in follows)
    itunes = ContentItunesSource().parse(
        Document(doc_id="i", source="content_itunes", body=Path("tests/fixtures/content/itunes.json").read_bytes()),
        acct,
        meta,
    )
    assert any(c.signal_type == "content_appearance" for c in itunes)

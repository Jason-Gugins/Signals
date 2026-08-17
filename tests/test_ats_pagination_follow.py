import json

from src.core.models import Account, Document
from src.sources.ats.collector import SmartRecruitersSource, WorkdaySource
from src.sources.ats.smartrecruiters import smartrecruiters_pages
from src.sources.ats.workday import workday_offsets


def test_smartrecruiters_follow_skips_offset_zero():
    src = SmartRecruitersSource()
    body = json.dumps({"totalFound": 250, "content": []}).encode()
    doc = Document(
        doc_id="s",
        source=src.key,
        body=body,
        url="https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=0",
    )
    acct = Account(domain="acme.com", ats_vendor="smartrecruiters", ats_token="acme")
    follows = src.follow_tasks(doc, acct, {"token": "acme", "today": "2026-08-16"})
    offsets = sorted(int(t.meta["offset"]) for t in follows)
    assert offsets == [o for o in smartrecruiters_pages(250, 100) if o != 0]
    assert all("offset=" in t.url for t in follows)


def test_workday_follow_posts_remaining_offsets():
    src = WorkdaySource()
    body = json.dumps({"total": 45, "jobPostings": []}).encode()
    doc = Document(doc_id="w", source=src.key, body=body)
    acct = Account(domain="acme.com", ats_vendor="workday", ats_token="acme/wd5/acme")
    meta = {
        "today": "2026-08-16",
        "base": "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme",
        "token": "acme/wd5/acme",
    }
    follows = src.follow_tasks(doc, acct, meta)
    offs = [t.json_body["offset"] for t in follows]
    assert offs == [o for o in workday_offsets(45, 20) if o != 0]
    assert all(t.method == "POST" for t in follows)

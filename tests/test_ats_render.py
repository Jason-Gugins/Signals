"""src/sources/ats/render.py — ATS board payload -> careers text documents.

The careers harvest's Phase 1: the board API payloads are ALREADY stored
(greenhouse content=true, ashby descriptionHtml/descriptionPlain) — this
module renders them into prose documents the decide pipeline can gate and
measure (the G5 material wall is PROSE-BOUND; careers prose is the fix).
"""

from src.sources.ats.render import (
    careers_url_candidates,
    render_ashby_board,
    render_greenhouse_board,
)


def test_render_greenhouse_board_jobs_to_text_docs():
    payload = {"jobs": [
        {"title": "Community Manager",
         "absolute_url": "https://bubbleskincare.com/jobs/1",
         "location": {"name": "New York, NY"},
         "content": "<h2>Community Manager</h2><p>Location: New York, NY (Hybrid). "
                    "You will own our customer support experience across email, "
                    "chat and social, and help us pick our next helpdesk platform.</p>"},
        {"title": "No Content Role", "absolute_url": "https://x/jobs/2",
         "location": {"name": "Remote"}, "content": ""},
    ]}
    docs = render_greenhouse_board(payload, domain="bubbleskincare.com")
    assert len(docs) == 2
    d0 = docs[0]
    assert d0["domain"] == "bubbleskincare.com"
    assert d0["title"] == "Community Manager"
    assert "bubbleskincare.com/jobs/1" in d0["url"]
    assert "customer support experience" in d0["text"]
    assert "helpdesk platform" in d0["text"]
    assert "<h2>" not in d0["text"] and "&lt;" not in d0["text"]
    # a job with empty content still renders a title line (honest, thin)
    assert docs[1]["text"].startswith("No Content Role")


def test_render_ashby_board_prefers_description_plain():
    payload = {"jobs": [{
        "title": "Senior Support Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/brooklinen/abc",
        "locationName": None,
        "location": {"city": "New York"},
        "descriptionPlain": "You will run the helpdesk, triage tickets, and "
                            "evaluate whether our CX tooling still fits as we scale.",
        "descriptionHtml": "<p>html version</p>",
        "team": "Support",
    }]}
    docs = render_ashby_board(payload, domain="brooklinen.com")
    assert len(docs) == 1
    d = docs[0]
    assert "triage tickets" in d["text"]
    assert "html version" not in d["text"]      # plain preferred over html
    assert "Senior Support Engineer" in d["text"] and "Support" in d["text"]
    assert "brooklinen" in d["url"]


def test_careers_url_candidates():
    cands = careers_url_candidates("brumate.com")
    assert "https://brumate.com/careers" in cands
    assert "https://brumate.com/pages/careers" in cands
    assert any("about/careers" in c for c in cands)
    # stable order, no duplicates
    assert len(cands) == len(set(cands)) and cands == sorted(set(cands))

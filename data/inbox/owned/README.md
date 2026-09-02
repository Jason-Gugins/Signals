Drop CSV or JSONL here. Columns (any subset): timestamp, email, ip, page_url, event_type, session_id, count. (utm_source and campaign are accepted but currently unused by the ingest.)
event_type in {page_view, email_open, email_click, form_submit, demo_request, webinar_attend, doc_download, pricing_view}.
Files are read in place (not moved) — ingest is idempotent per run.

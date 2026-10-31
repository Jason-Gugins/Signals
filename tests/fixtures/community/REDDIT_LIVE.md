# REDDIT_LIVE.md — provenance for reddit_search_live.rss

- Captured: 2026-10-31T03:12:01+00:00 (documents.fetched_at)
- How: `python -m src.cli collect --source community_reddit --force --domain brooklinen.com`
- URL: https://www.reddit.com/search.rss?q=%22brooklinen%22&sort=new&t=week
- HTTP 200, content-type application/atom+xml; charset=UTF-8, 7721 bytes
- Path: fetched through the scoped robots_allow override (config/default.yaml,
  www.reddit.com + /search.rss) — operator-approved 2026-10-30, see that file
- Why frozen: live-capture drift fix (Task 16) — the feed mixes t5_ SUBREDDIT
  entries (id t5_*, url = subreddit home, sidebar/description may mention the
  brand) with t3_ post entries. The synthetic fixture never had this class;
  reddit_to_candidates now emits t3_ entries only. Also pins: entry category
  tags are CLEAN terms ("BuyItForLife", no leading space — the feed-level
  category has one; parser strips either way).
- Re-freeze when Reddit's Atom shape drifts again (re-run the collect above
  and swap the .rss; the tests here will tell you).

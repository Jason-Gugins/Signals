"""Guard for the scoped Reddit robots override (plan Task 15, D2).

Operator decision (2026-10-30): reddit robots.txt is `User-agent: *
Disallow: /` and the Public Content Policy restricts automated collection,
but /search.rss is a public syndication path (HTTP 200 Atom, probe
2026-10-30). The override lives honestly in config/default.yaml and must
stay scoped to ONE path; community_reddit ships enabled at trickle volume.
"""

from pathlib import Path

import yaml


def test_reddit_override_is_scoped_and_enabled():
    cfg = yaml.safe_load(Path("config/default.yaml").read_text(encoding="utf-8"))
    entries = [e for e in cfg["http"]["robots_allow"] if e["host"] == "www.reddit.com"]
    assert len(entries) == 1 and entries[0]["path_prefix"] == "/search.rss"  # ONE path only
    src = yaml.safe_load(Path("config/sources.yaml").read_text(encoding="utf-8"))
    assert src["sources"]["community_reddit"]["enabled"] is True  # operator-approved override (2026-10-30)
    assert src["sources"]["community_reddit"]["rate_per_host"] <= 0.2  # trickle volume is part of the decision

"""App-store review sources (Task 9, P2 wave-1).

App Store: REAL GO per the P2 spike (data/probe/P2_SOURCE_SPIKE.md) — the
iTunes customer-reviews RSS feed is public server-rendered JSON with no
anti-bot, fetched plain (curl_cffi / http tier).

Play Store: SYNTHETIC-ONLY per the spike (JS-rendered reviews page; the
probed package 404'd). Do NOT build a Play fetcher — any future Play
review capture must go through a browser-tier probe first.
"""

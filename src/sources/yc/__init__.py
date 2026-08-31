"""YC directory source (STUB — fixture-only).

SPIKE VERDICT (data/probe/P2_SOURCE_SPIKE.md, 2026-08-31): the YC companies
directory is a client-rendered Inertia.js shell. The server response carries
only a ``div[data-page]`` mount-point JSON whose props contain ``env`` and
``currentBatch`` — the companies list loads via further async calls and is NOT
extractable server-side. This parser handles the Inertia ``data-page`` shape
(exercised by a synthetic fixture); going live requires a browser-tier upgrade.
"""

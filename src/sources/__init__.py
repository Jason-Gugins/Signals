"""Import collectors so @register populates SOURCES."""

from importlib import import_module

_PACKAGES = (
    "src.sources.sec.collector",
    "src.sources.ats.collector",
    "src.sources.news.collector",
    "src.sources.regulatory.collector",
    "src.sources.techstack.collector",
    "src.sources.wayback.collector",
    "src.sources.crtsh.collector",
    "src.sources.community.collector",
    "src.sources.marketplace.collector",
    "src.sources.content.collector",
    "src.sources.owned.collector",
    "src.sources.linkedin_db.collector",
)

for _mod in _PACKAGES:
    try:
        import_module(_mod)
    except Exception:
        pass

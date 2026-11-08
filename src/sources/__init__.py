"""Import collectors so @register populates SOURCES."""

# (appstore_reviews — live iTunes RSS)
import src.sources.appstores.appstore

# (ats_breezy)
import src.sources.ats.breezy

# (ats_careers_page — no-ATS fallback)
import src.sources.ats.careers_page
import src.sources.ats.collector

# (ats_jobvite)
import src.sources.ats.jobvite

# (ats_rippling)
import src.sources.ats.rippling

# (ats_teamtailor)
import src.sources.ats.teamtailor

# (bbb_profile — live SSR)
import src.sources.bbb.collector
import src.sources.community.collector

# (community_reddit — live via scoped /search.rss robots override, 2026-10-30)
import src.sources.community.reddit
import src.sources.content.collector

# (content_producthunt — stub)
import src.sources.content.producthunt
import src.sources.crtsh.collector

# (federal_contracts — live usaspending.gov award search)
import src.sources.federal.collector
import src.sources.jobsignals.collector
import src.sources.linkedin_db.collector
import src.sources.marketplace.collector

# (marketplace_softwareadvice / marketplace_getapp — disabled by default)
import src.sources.marketplace.sa_ga

# (needs — local-tier derived, profile-matched)
import src.sources.needs.collector
import src.sources.news.collector
import src.sources.owned.collector
import src.sources.regulatory.collector
import src.sources.repvue_db.collector
import src.sources.sec.collector
import src.sources.sec.formd_source

# (sitecontent — sitemap-enumerated prose pages, spike arm C)
import src.sources.sitecontent.collector
import src.sources.techstack.collector
import src.sources.warn.source
import src.sources.wayback.collector
import src.sources.yc.collector  # noqa: F401  (yc_batch — stub, browser-tier upgrade required)

"""Import collectors so @register populates SOURCES."""

import src.sources.sec.collector  # noqa: F401
import src.sources.sec.formd_source  # noqa: F401
import src.sources.ats.collector  # noqa: F401
import src.sources.jobsignals.collector  # noqa: F401
import src.sources.news.collector  # noqa: F401
import src.sources.regulatory.collector  # noqa: F401
import src.sources.warn.source  # noqa: F401
import src.sources.techstack.collector  # noqa: F401
import src.sources.wayback.collector  # noqa: F401
import src.sources.crtsh.collector  # noqa: F401
import src.sources.community.collector  # noqa: F401
import src.sources.community.reddit  # noqa: F401  (community_reddit — disabled-by-default)
import src.sources.marketplace.collector  # noqa: F401
import src.sources.content.collector  # noqa: F401
import src.sources.content.producthunt  # noqa: F401  (content_producthunt — stub)
import src.sources.yc.collector  # noqa: F401  (yc_batch — stub, browser-tier upgrade required)
import src.sources.appstores.appstore  # noqa: F401  (appstore_reviews — live iTunes RSS)
import src.sources.bbb.collector  # noqa: F401  (bbb_profile — live SSR)
import src.sources.owned.collector  # noqa: F401
import src.sources.linkedin_db.collector  # noqa: F401
import src.sources.repvue_db.collector  # noqa: F401

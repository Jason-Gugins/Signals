"""Read-only LinkedIn DB adapter + optional deepen subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.core.config import Config
from src.sources.base import SourceAdapter
from src.sources.registry import register


@register
class LinkedinDbSource(SourceAdapter):
    key = "linkedin_db"
    tier = "local"
    cadence_hours = 6

    def plan(self, account, cursor):
        return []

    def parse(self, doc, account, task_meta):
        return []

    def local_harvest(self, *, db, account, today, task_meta):
        from src.core.config import Config
        from src.identity.seeds import _open_ro
        from src.sources.linkedin_db.jobs import linkedin_jobs_to_candidates
        from src.sources.linkedin_db.people import detect_role_changes, people_to_contacts
        from src.sources.linkedin_db.posts import posts_to_candidates

        cfg = task_meta.get("config") or Config()
        path = cfg.external_dbs.linkedin_db
        if not Path(path).exists():
            return []
        try:
            conn = _open_ro(path)
        except Exception:
            return []
        try:
            slug = account.linkedin_slug
            people = []
            jobs = []
            posts = []
            if slug:
                try:
                    people = [dict(r) for r in conn.execute("SELECT * FROM people WHERE company_slug=?", (slug,)).fetchall()]
                except Exception:
                    people = []
                try:
                    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE company_slug=?", (slug,)).fetchall()]
                except Exception:
                    jobs = []
                try:
                    posts = [dict(r) for r in conn.execute("SELECT * FROM posts WHERE author_slug=?", (slug,)).fetchall()]
                except Exception:
                    posts = []
        finally:
            conn.close()
        contacts = people_to_contacts(people, account.domain)
        champs = {c.linkedin_slug: {"prior_domain": c.prior_domain, "prior_company": None} for c in contacts if c.is_champion}
        out = []
        out.extend(detect_role_changes(contacts, people, champs, today=today))
        out.extend(linkedin_jobs_to_candidates(jobs, account.domain, today=today))
        out.extend(posts_to_candidates(posts, account.domain, contacts, today=today))
        return out


def request_linkedin_deepen(config: Config, slug: str, **kw):
    try:
        exe = Path(config.external_dbs.linkedin_cli_cwd) / ".venv" / "Scripts" / "python.exe"
        url = f"https://www.linkedin.com/company/{slug}/"
        log = Path("data/logs") / f"deepen_{slug}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [str(exe), "-m", "src.cli", "enrich", "--company-url", url],
            cwd=config.external_dbs.linkedin_cli_cwd,
            timeout=kw.get("timeout", 900),
            capture_output=True,
            text=True,
        )
        log.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        return proc
    except Exception as exc:
        from loguru import logger
        logger.warning("linkedin deepen failed for {}: {}", slug, exc)
        return None

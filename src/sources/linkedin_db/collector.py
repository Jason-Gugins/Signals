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

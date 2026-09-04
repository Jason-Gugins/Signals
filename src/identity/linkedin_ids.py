"""Fill account.linkedin_slug by delegating to the companion LinkedIn scraper.

Intended dispatch: wired into orchestrator.resolve() by the parent as a
`linkedin`-flagged resolver (this module does not self-dispatch).

Design (mirrors request_linkedin_deepen in src/sources/linkedin_db/collector.py):
the scraper is a sibling checkout (config.external_dbs.linkedin_cli_cwd) with
its own venv. `discover` has no single-company mode — only a --seed CSV — so
batching ALL accounts needing a slug into ONE one-row-per-account seed CSV and
running a single subprocess per resolve_all() call keeps us inside the
scraper's internal pacing. The resolved slug is read from the scraper's own
sqlite DB (data/linkedin.db, companies table) keyed by domain.

Failure policy: any subprocess error, timeout, or missing DB row yields None
plus a warning telling the operator to run the scraper's login manually.
Never passes --headed and never invokes login — it relies on the existing
session file. Only account.linkedin_slug is stored (Account has no
linkedin_url column and nothing consumes one).
"""

from __future__ import annotations

import csv
import sqlite3
import subprocess
from pathlib import Path

from loguru import logger

from src.core.config import Config
from src.core.models import Account

COMPANIES_QUERY = (
    "SELECT linkedin_url, linkedin_slug, domain FROM companies "
    "WHERE domain=? ORDER BY rowid DESC LIMIT 1"
)


class LinkedinSlugResolver:
    """Runs the scraper's `discover` over a seed CSV, then reads slugs from its DB."""

    def __init__(self, config: Config, registry, timeout: int = 600):
        self.config = config
        self.registry = registry
        self.timeout = timeout

    def resolve_all(self, accounts: list[Account]) -> dict[str, str | None]:
        cwd = Path(self.config.external_dbs.linkedin_cli_cwd)
        exe = cwd / ".venv" / "Scripts" / "python.exe"
        if not cwd.exists() or not exe.exists():
            logger.warning(
                "linkedin scraper checkout not found at {} — slug resolution skipped", cwd
            )
            return {}

        out: dict[str, str | None] = {}
        need: list[Account] = []
        for acct in accounts:
            if acct.linkedin_slug:
                out[acct.domain] = acct.linkedin_slug
            else:
                need.append(acct)
        if not need:
            return out

        seed_path: Path | None = None
        try:
            seed_path = self._write_seed_csv(need)
            ok = self._run_discover(cwd, exe, seed_path)
            for acct in need:
                slug = self._lookup_slug(cwd, acct) if ok else None
                out[acct.domain] = slug
                if slug:
                    acct.linkedin_slug = slug
                    self.registry.upsert(acct, source="linkedin_ids")
            return out
        finally:
            # The seed CSV carries account names — never leave it on disk.
            if seed_path is not None:
                try:
                    seed_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove seed CSV {}", seed_path)

    def _write_seed_csv(self, accounts: list[Account]) -> Path:
        log_dir = Path("data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "linkedin_seed.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["name", "domain"])
            for acct in accounts:
                writer.writerow([(acct.name or "").strip(), acct.domain])
        return path

    def _run_discover(self, cwd: Path, exe: Path, seed_path: Path) -> bool:
        argv = [str(exe), "-m", "src.cli", "discover", "--seed", str(seed_path)]
        log_path = Path("data/logs") / "linkedin_discover.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                timeout=self.timeout,
                capture_output=True,
                text=True,
            )
            log_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
            if proc.returncode != 0:
                logger.warning(
                    "linkedin discover exited {} — see {}; run the scraper's login manually",
                    proc.returncode,
                    log_path,
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                "linkedin discover timed out after {}s — run the scraper's login manually",
                self.timeout,
            )
            return False
        except Exception as exc:
            logger.warning(
                "linkedin discover failed: {} — run the scraper's login manually", exc
            )
            return False

    def _lookup_slug(self, cwd: Path, account: Account) -> str | None:
        db_path = cwd / "data" / "linkedin.db"
        if not db_path.exists():
            logger.warning(
                "linkedin scraper DB missing at {} — run the scraper's login manually", db_path
            )
            return None
        try:
            # timeout=30: the scraper process may hold the DB mid-write right
            # after discover returns; default 5s busy-timeout is too tight.
            conn = sqlite3.connect(str(db_path), timeout=30)
            try:
                row = conn.execute(COMPANIES_QUERY, (account.domain,)).fetchone()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "linkedin slug lookup failed for {}: {} — run the scraper's login manually",
                account.domain,
                exc,
            )
            return None
        if not row:
            return None
        return row[1] or None

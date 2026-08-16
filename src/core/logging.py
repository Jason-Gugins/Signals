"""loguru setup with stderr + rotating file sinks. Idempotent."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.core.config import Config

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "{extra[run_id]} | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging(config: Config, run_id: str | None = None) -> None:
    """Removes default sink; adds stderr + rotating file. Idempotent."""
    logger.remove()
    bound_id = run_id or "-"
    logger.configure(extra={"run_id": bound_id})
    level = config.logging.level
    logger.add(sys.stderr, level=level, format=_FORMAT)
    log_path = Path(config.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_path),
        level=level,
        format=_FORMAT,
        rotation=config.logging.rotation,
    )
    if run_id:
        logger.bind(run_id=run_id)

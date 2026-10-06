"""Small standard-library logging setup."""

from __future__ import annotations

import logging
import sys

from receptionist.core.config import LogLevel, Settings

LOGGER = logging.getLogger("receptionist")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: LogLevel) -> None:
    """Configure readable process logging to stdout."""
    logging.basicConfig(
        level=getattr(logging, level),
        format=LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )


def log_configuration(settings: Settings) -> None:
    """Log only the safe configuration summary."""
    LOGGER.info("Application configuration loaded: %s", settings.safe_summary())


def log_startup() -> None:
    """Log application startup without runtime secrets."""
    LOGGER.info("Application startup")


def log_shutdown() -> None:
    """Log application shutdown."""
    LOGGER.info("Application shutdown")

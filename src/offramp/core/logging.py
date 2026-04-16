"""Structured logging setup.

All packages MUST import :func:`get_logger` instead of using ``print`` or the
stdlib root logger directly. Format defaults to JSON (production); set
``LOG_FORMAT=console`` for human-readable dev output.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from offramp.core.config import get_settings

_configured = False


def configure_logging() -> None:
    """Idempotent global logging config.

    Called once on process start (tests, CLI entry points, services).
    """
    global _configured
    if _configured:
        return

    settings = get_settings().observability
    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger; lazily configures global state.

    Return type is ``Any`` because structlog's ``BoundLogger`` interface is
    dynamic — concrete types depend on the configured processor chain.
    """
    configure_logging()
    return structlog.get_logger(name)

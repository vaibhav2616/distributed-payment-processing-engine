"""
core/logging.py
----------------
Structlog configuration for structured, JSON-formatted logging.

Features:
  - JSON output in production (LOG_FORMAT=json, default)
  - Human-readable console output in development (LOG_FORMAT=console)
  - Automatic correlation_id injection via contextvars
  - ISO 8601 timestamps
  - Callsite information (file, function, line number)
  - Log level configurable via LOG_LEVEL env var

Usage:
  Call ``configure_logging()`` once at application startup (in create_app).
  Then in any module:
      import structlog
      logger = structlog.get_logger(__name__)
      logger.info("payment_created", transaction_id="txn_123", amount=99.99)
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Context variable — injected by CorrelationIdMiddleware on each request
# ---------------------------------------------------------------------------
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")


def _add_correlation_id(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor that injects the current correlation_id."""
    cid = correlation_id_ctx.get("")
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """
    Bootstrap structlog.  Call exactly once at application startup.

    Args:
        log_level:  Standard Python log level string (DEBUG/INFO/WARNING/ERROR).
        log_format: ``"json"`` for machine-readable output (production default),
                    ``"console"`` for pretty human-readable output (development).
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        ),
    ]

    if log_format == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

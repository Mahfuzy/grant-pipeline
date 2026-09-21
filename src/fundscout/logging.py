"""Structured logging (SPEC §13).

Context such as `source`, `run_id` and `url` is bound with `structlog.contextvars`
so every log line emitted while processing an item carries it.
"""

import logging
import sys

import structlog


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    return structlog.PrintLogger(sys.stderr)


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        # Resolve sys.stderr at call time (it may be swapped, e.g. by test runners).
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger

"""Structured logging for Cadence.

A thin wrapper over the stdlib :mod:`logging` that emits single-line JSON records so
downstream log shippers can parse them. No third-party dependency. Never log verbatim
raw content — log ids/hashes/summaries only (the raw boundary applies to logs too).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Formats log records as compact single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root ``cadence`` logger once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("cadence")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced ``cadence.*`` logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(f"cadence.{name}")


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Log ``msg`` at ``level`` with structured ``fields`` attached to the JSON record."""
    logger.log(level, msg, extra={"extra_fields": fields})


__all__ = ["configure_logging", "get_logger", "log_event", "JsonFormatter"]

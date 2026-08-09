"""Logging configuration.

Human-readable on a TTY, JSON when `FWCOPILOT_LOG_FORMAT=json` — the latter is
what you want when the server runs under systemd, Docker or a log shipper.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

LOGGER_NAME = "fwcopilot"
_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    COLORS: ClassVar[Dict[str, str]] = {
        "DEBUG": "2",
        "INFO": "36",
        "WARNING": "33",
        "ERROR": "31",
        "CRITICAL": "31;1",
    }

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.lower()
        if self.color:
            code = self.COLORS.get(record.levelname, "0")
            level = f"\033[{code}m{level}\033[0m"
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{level:>8} {message}"


def configure_logging(
    verbosity: int = 0,
    *,
    quiet: bool = False,
    log_file: Optional[Path] = None,
    fmt: Optional[str] = None,
) -> logging.Logger:
    """Set up the `fwcopilot` logger. Safe to call more than once."""
    global _CONFIGURED

    level = logging.WARNING
    if quiet:
        level = logging.ERROR
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO

    env_level = os.environ.get("FWCOPILOT_LOG_LEVEL")
    if env_level:
        level = getattr(logging, env_level.upper(), level)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    fmt = fmt or os.environ.get("FWCOPILOT_LOG_FORMAT", "human")
    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else HumanFormatter(color=sys.stderr.isatty())
    )
    logger.addHandler(handler)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)

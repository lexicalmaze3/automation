"""Structured logging to stdout + a rotating file.

Every log line is emitted as a single JSON object so the output is trivially
grep-/jq-able in CI and in aggregators.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any structured extras passed via logger.info(..., extra={"extra": {...}})
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_file: str = "scraper.log", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("scraper")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Helper: emit a message with structured extra fields."""
    logger.log(level, msg, extra={"extra": fields})


def alert(logger: logging.Logger, msg: str, **fields) -> None:
    """Fire a loud, unmissable alert line.

    Stage 2: this is a CRITICAL log tagged alert=true. Stage 5 hooks a Discord
    webhook into the same call site so alerts also leave the machine.
    """
    fields["alert"] = True
    logger.log(logging.CRITICAL, msg, extra={"extra": fields})

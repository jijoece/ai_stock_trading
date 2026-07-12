"""stderr-only logging with credential redaction (docs/milestone-4.md Step 5).

Protocol traffic is stdout-only (one JSON line per response); every log
record here goes to stderr so the main process's stdout reader never has to
distinguish a log line from a protocol response. Credential values are
redacted from every log message even if a caller accidentally interpolates
one.
"""
from __future__ import annotations

import logging
import os
import sys

_SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_OAUTH_TOKEN")


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for name in _SECRET_ENV_VARS:
            value = os.environ.get(name)
            if value and value in message:
                message = message.replace(value, "***REDACTED***")
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("trading_paper_runtime")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        handler.addFilter(_RedactingFilter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger

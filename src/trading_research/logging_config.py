"""Structured logging with centralized secret redaction.

No API key, bearer token, OAuth token, account number, or raw credential
header may reach a log line. redact() is applied to every formatted message.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(sk-ant-[A-Za-z0-9\-_]{10,})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9\-_.]{10,})", re.IGNORECASE),
    re.compile(r"(\"?(?:api[_-]?key|token|secret|password|authorization)\"?\s*[:=]\s*\"?)([^\s\"',}]{4,})", re.IGNORECASE),
]

_RUNTIME_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Register an in-memory secret value so it can be scrubbed from logs verbatim."""
    if value and value not in _RUNTIME_SECRETS:
        _RUNTIME_SECRETS.append(value)


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for secret in _RUNTIME_SECRETS:
        if secret and secret in out:
            out = out.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) + "[REDACTED]") if m.lastindex and m.lastindex >= 2 else "[REDACTED]", out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact(original)


class JsonRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("run_id", "workstream_id", "batch_id", "custom_id", "operation", "status", "duration_ms", "error_type"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = redact(str(value)) if isinstance(value, str) else value
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    root = logging.getLogger("trading_research")
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        JsonRedactingFormatter() if json_output else RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        )
    )
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trading_research.{name}")

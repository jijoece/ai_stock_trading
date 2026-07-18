"""Version-specific JSONL adapter for `codex exec --json` (event shapes
captured against codex-cli 0.144.5 on 2026-07-18; see
docs/codex-production-provider.md's "JSONL parsing" section).

Encapsulating the exact event shapes here — rather than in
`codex_provider.py` — means a future Codex CLI version bump only requires
updating this module, not the orchestration-facing provider code (Milestone
12: "If the JSONL contract is not sufficiently stable, encapsulate all
version-specific parsing in a small adapter").

Understood event types, confirmed against a live smoke test (never persisted
raw):

    {"type": "thread.started", "thread_id": "<uuid>"}
    {"type": "turn.started"}
    {"type": "item.completed", "item": {"id": ..., "type": "agent_message", "text": "<json>"}}
    {"type": "item.completed", "item": {"id": ..., "type": "error", "message": "..."}}
    {"type": "error", "message": "..."}
    {"type": "turn.completed", "usage": {"input_tokens": N, "cached_input_tokens": N,
                                          "output_tokens": N, "reasoning_output_tokens": N}}
    {"type": "turn.failed", "error": {"message": "..."}}

`turn.completed` and `turn.failed` are the only terminal events — exactly
one must appear, and nothing may follow it. This parser is intentionally
strict: an event type it does not recognize fails closed rather than being
silently skipped, and it never scans arbitrary nested JSON for a
plausible-looking answer or token count.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import MalformedOutputError

_TERMINAL_TYPES = ("turn.completed", "turn.failed")
_RECOGNIZED_TYPES = ("thread.started", "turn.started", "item.completed", "error") + _TERMINAL_TYPES


@dataclass(frozen=True)
class CodexUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None
    reasoning_output_tokens: int | None


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str | None
    agent_message_text: str | None
    usage: CodexUsage | None
    succeeded: bool
    failure_message: str | None
    event_count: int


def _fail(code: str, message: str, *, retryable: bool = True) -> MalformedOutputError:
    return MalformedOutputError(message, code=code, retryable=retryable)


def _parse_usage(usage_obj: object) -> CodexUsage:
    if not isinstance(usage_obj, dict):
        raise _fail("CODEX_USAGE_METADATA_MISSING", "Codex turn.completed event did not contain usage metadata", retryable=False)
    input_tokens = usage_obj.get("input_tokens")
    output_tokens = usage_obj.get("output_tokens")
    cached_input_tokens = usage_obj.get("cached_input_tokens")
    reasoning_output_tokens = usage_obj.get("reasoning_output_tokens")
    if type(input_tokens) is not int or input_tokens < 0:
        raise _fail("CODEX_USAGE_METADATA_MISSING", "Codex usage.input_tokens was missing or malformed", retryable=False)
    if type(output_tokens) is not int or output_tokens < 0:
        raise _fail("CODEX_USAGE_METADATA_MISSING", "Codex usage.output_tokens was missing or malformed", retryable=False)
    for name, value in (
        ("cached_input_tokens", cached_input_tokens), ("reasoning_output_tokens", reasoning_output_tokens),
    ):
        if value is not None and (type(value) is not int or value < 0):
            raise _fail("CODEX_USAGE_METADATA_MISSING", f"Codex usage.{name} was malformed", retryable=False)
    return CodexUsage(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens, reasoning_output_tokens=reasoning_output_tokens,
    )


def parse_codex_jsonl(raw: bytes, *, maximum_line_bytes: int, maximum_event_count: int) -> CodexTurnResult:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("CODEX_INVALID_JSONL", "Codex output was not valid UTF-8") from exc

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # a single trailing newline is normal JSONL framing, not an event

    thread_id: str | None = None
    agent_message_text: str | None = None
    usage: CodexUsage | None = None
    terminal_seen = False
    succeeded = False
    failure_message: str | None = None
    event_count = 0

    for line in lines:
        if not line.strip():
            raise _fail("CODEX_INVALID_JSONL", "Codex output contained a blank line")
        if len(line.encode("utf-8")) > maximum_line_bytes:
            raise _fail("CODEX_INVALID_JSONL", "Codex JSONL line exceeded the configured byte limit", retryable=False)
        event_count += 1
        if event_count > maximum_event_count:
            raise _fail("CODEX_INVALID_JSONL", "Codex emitted more JSONL events than the configured limit", retryable=False)

        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _fail("CODEX_INVALID_JSONL", "Codex emitted a malformed JSONL line") from exc
        if not isinstance(event, dict):
            raise _fail("CODEX_INVALID_JSONL", "Codex JSONL event was not an object")
        event_type = event.get("type")
        if event_type not in _RECOGNIZED_TYPES:
            raise _fail("CODEX_INVALID_JSONL", "Codex emitted an unrecognized event type")

        if terminal_seen:
            if event_type in _TERMINAL_TYPES:
                raise _fail("CODEX_MULTIPLE_TERMINAL_EVENTS", "Codex emitted more than one terminal event", retryable=False)
            raise _fail("CODEX_INVALID_JSONL", "Codex emitted an event after the terminal event", retryable=False)

        if event_type == "thread.started":
            value = event.get("thread_id")
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise _fail("CODEX_INVALID_JSONL", "Codex thread_id was malformed")
                thread_id = value
        elif event_type == "turn.started":
            pass
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise _fail("CODEX_INVALID_JSONL", "Codex item.completed event was malformed")
            if item.get("type") == "agent_message":
                text_value = item.get("text")
                if not isinstance(text_value, str):
                    raise _fail("CODEX_INVALID_JSONL", "Codex agent_message item had no text")
                # The final agent_message before the terminal event is the
                # answer — a later one (if the model ever emits more than
                # one) replaces the earlier, never accumulated/concatenated.
                agent_message_text = text_value
        elif event_type == "error":
            pass  # non-terminal advisory event; the terminal turn.failed carries the authoritative message
        elif event_type == "turn.completed":
            terminal_seen = True
            succeeded = True
            usage = _parse_usage(event.get("usage"))
        elif event_type == "turn.failed":
            terminal_seen = True
            succeeded = False
            error_obj = event.get("error")
            message = error_obj.get("message") if isinstance(error_obj, dict) else None
            failure_message = message if isinstance(message, str) and message.strip() else "Codex turn failed"

    if not terminal_seen:
        raise _fail("CODEX_TERMINAL_EVENT_MISSING", "Codex output did not contain a terminal turn.completed/turn.failed event")

    return CodexTurnResult(
        thread_id=thread_id, agent_message_text=agent_message_text, usage=usage,
        succeeded=succeeded, failure_message=failure_message, event_count=event_count,
    )


__all__ = ["CodexTurnResult", "CodexUsage", "parse_codex_jsonl"]

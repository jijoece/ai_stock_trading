"""Sanitized JSONL event fixtures for the Codex CLI's `codex exec --json`
stream (docs/milestone-12-codex-provider.md: "Treat the installed JSONL
structure as versioned external input. Add sanitized fixtures representing
the required event shapes.").

Captured against codex-cli 0.144.5 (npm `@openai/codex`) on 2026-07-18 via a
harmless, temporary structured-output smoke test that was never persisted —
only the event *shape* below is derived from that run; no real prompt,
response content, credential, or model reasoning appears here.
"""
from __future__ import annotations

import json


def successful_turn_jsonl(
    final_response_json_text: str,
    *,
    thread_id: str = "019f741d-7336-74e0-9d80-f73e133f2393",
    input_tokens: int = 13226,
    cached_input_tokens: int = 0,
    output_tokens: int = 15,
    reasoning_output_tokens: int = 0,
) -> str:
    """The normal-path event sequence: thread.started -> turn.started ->
    one item.completed(agent_message) -> turn.completed(usage)."""
    lines = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": final_response_json_text}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens, "reasoning_output_tokens": reasoning_output_tokens,
            },
        },
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def failed_turn_jsonl(
    *,
    thread_id: str = "019f741e-caf2-7e22-8046-91c26ce1ae07",
    error_message: str = '{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"the requested model is not supported"}}',
) -> str:
    """The observed failure-path event sequence: thread.started -> an
    advisory item.completed(error) -> turn.started -> a standalone error
    event -> the terminal turn.failed."""
    lines = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "model metadata not found; using fallback"}},
        {"type": "turn.started"},
        {"type": "error", "message": error_message},
        {"type": "turn.failed", "error": {"message": error_message}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


__all__ = ["failed_turn_jsonl", "successful_turn_jsonl"]

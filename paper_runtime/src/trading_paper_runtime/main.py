"""stdin/stdout JSON Lines entry point (docs/milestone-4.md Step 2/4).

Run as `python -m trading_paper_runtime`. Reads one request object per
line from stdin, writes one response object per line to stdout, and exits
when stdin closes. All logging goes to stderr — stdout carries protocol
traffic only.
"""
from __future__ import annotations

import sys

from . import RUNTIME_VERSION
from .configuration import load_runtime_configuration
from .dispatcher import Dispatcher
from .errors import RuntimeOperationError
from .logging_config import configure_logging
from .protocol import build_error_response, build_success_response, parse_request_line


def _build_gateway(config):
    from .lumibot_gateway import LumiBotAlpacaPaperGateway

    return LumiBotAlpacaPaperGateway(config=config)


def run(stdin=None, stdout=None, gateway_factory=_build_gateway) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    logger = configure_logging()

    # LumiBot logs an unguarded startup banner directly to stdout at import
    # time (confirmed: "LumiBot v4.5.74 starting", verified via manual
    # smoke test of this CLI's `paper-runtime-health` command) — anything
    # written through `sys.stdout` after this point (by LumiBot or any
    # other imported library) must go to stderr instead, since stdout is
    # reserved exclusively for paper-runtime.v1 JSON Lines responses,
    # written below via the captured `stdout` handle directly, never via
    # `print`/`sys.stdout`.
    sys.stdout = sys.stderr

    config = load_runtime_configuration()
    gateway = gateway_factory(config)
    dispatcher = Dispatcher(gateway=gateway, config=config)

    logger.info("trading-paper-runtime starting (runtime_version=%s)", RUNTIME_VERSION)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request_id = "unknown"
        operation = "unknown"
        try:
            request = parse_request_line(line)
            request_id, operation = request.request_id, request.operation
            payload = dispatcher.handle(request)
            response = build_success_response(request, runtime_version=RUNTIME_VERSION, payload=payload)
        except RuntimeOperationError as exc:
            logger.warning("request failed: code=%s message=%s", exc.code, exc.message)
            response = build_error_response(
                request_id, operation, runtime_version=RUNTIME_VERSION, error=exc,
            )
        except Exception as exc:  # never let an unexpected exception crash the read loop
            logger.exception("unexpected error handling request")
            from .errors import ErrorCode

            response = build_error_response(
                request_id, operation, runtime_version=RUNTIME_VERSION,
                error=RuntimeOperationError(ErrorCode.INTERNAL_ERROR, str(exc), retryable=False),
            )
        stdout.write(response.to_json_line() + "\n")
        stdout.flush()

    logger.info("trading-paper-runtime stdin closed, exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

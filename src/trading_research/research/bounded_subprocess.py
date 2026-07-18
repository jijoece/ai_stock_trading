"""Provider-neutral hardened subprocess runner, extracted from
``claude_code_provider.py`` so a second locked-down CLI provider (Codex) can
reuse the exact same process-isolation guarantees instead of a second,
potentially weaker implementation.

Everything here is provider-agnostic: absolute-executable invocation is the
caller's responsibility (this module only ever receives an already-built
``argv``), ``shell=False``, a new process group, bounded stdin/stdout/stderr,
a wall-clock timeout, SIGTERM followed by bounded SIGKILL, and byte-limited
output. Provider-specific parsing, error classification, and environment
allowlists stay in each provider's own module.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    latency_ms: int


class ProcessOutputOverflow(Exception):
    """``stdout`` or ``stderr`` exceeded its configured byte limit."""

    def __init__(self, stream_name: str, stdout_bytes: int, stderr_bytes: int):
        super().__init__(stream_name)
        self.stream_name = stream_name
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes


class ProcessTimeoutError(Exception):
    """The subprocess did not exit within ``request_timeout_seconds``."""

    def __init__(self, latency_ms: int):
        super().__init__("subprocess timed out")
        self.latency_ms = latency_ms


class ProcessShutdownError(Exception):
    """The subprocess or its I/O pump threads did not shut down cleanly."""


@dataclass(frozen=True)
class BoundedProcessConfig:
    working_directory: Path
    request_timeout_seconds: int
    terminate_grace_seconds: int
    maximum_stdout_bytes: int
    maximum_stderr_bytes: int


def terminate_process_group(process: "subprocess.Popen[bytes]", grace_seconds: int) -> None:
    """SIGTERM the process group, wait up to ``grace_seconds``, then SIGKILL."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=grace_seconds)
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        # A direct child should always be reapable after SIGKILL. Keep this
        # bounded and let the caller surface a safe unavailable error.
        pass


class BoundedProcessRunner:
    """Runs one absolute-executable ``argv`` with bounded stdin/stdout/stderr,
    a wall-clock timeout, and guaranteed process-group cleanup. Never invoked
    through a shell, never inherits the parent environment implicitly — the
    caller always supplies an already-sanitized ``env``."""

    def __init__(self, config: BoundedProcessConfig):
        self._config = config

    def run(self, argv: list[str], *, env: Mapping[str, str], stdin_data: bytes = b"") -> BoundedProcessResult:
        started = time.monotonic()
        process: "subprocess.Popen[bytes] | None" = None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        overflow = threading.Event()
        overflow_stream: list[str] = []
        pump_errors: list[BaseException] = []

        def pump(pipe, target: bytearray, limit: int, name: str) -> None:
            try:
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        return
                    remaining = limit - len(target)
                    if remaining > 0:
                        target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow_stream.append(name)
                        overflow.set()
                        return
            except BaseException as exc:  # cleaned up and mapped by the owning thread
                pump_errors.append(exc)

        def write_stdin(pipe) -> None:
            try:
                if stdin_data:
                    pipe.write(stdin_data)
                    pipe.flush()
            except BrokenPipeError:
                pass
            except BaseException as exc:
                pump_errors.append(exc)
            finally:
                try:
                    pipe.close()
                except BaseException:
                    pass

        threads: list[threading.Thread] = []
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._config.working_directory,
                env=dict(env),
                shell=False,
                start_new_session=True,
            )
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            threads = [
                threading.Thread(target=pump, args=(process.stdout, stdout_buffer, self._config.maximum_stdout_bytes, "stdout"), daemon=False),
                threading.Thread(target=pump, args=(process.stderr, stderr_buffer, self._config.maximum_stderr_bytes, "stderr"), daemon=False),
                threading.Thread(target=write_stdin, args=(process.stdin,), daemon=False),
            ]
            for thread in threads:
                thread.start()

            deadline = started + self._config.request_timeout_seconds
            while process.poll() is None:
                if overflow.is_set():
                    terminate_process_group(process, self._config.terminate_grace_seconds)
                    break
                if time.monotonic() >= deadline:
                    terminate_process_group(process, self._config.terminate_grace_seconds)
                    for thread in threads:
                        thread.join(timeout=self._config.terminate_grace_seconds)
                    raise ProcessTimeoutError(latency_ms=int((time.monotonic() - started) * 1000))
                time.sleep(0.01)

            if process.poll() is None:
                terminate_process_group(process, self._config.terminate_grace_seconds)
            process.wait(timeout=self._config.terminate_grace_seconds)
            for thread in threads:
                thread.join(timeout=self._config.terminate_grace_seconds)
            if any(thread.is_alive() for thread in threads):
                raise ProcessShutdownError("subprocess I/O did not shut down cleanly")
            if overflow_stream:
                raise ProcessOutputOverflow(overflow_stream[0], len(stdout_buffer), len(stderr_buffer))
            if pump_errors:
                raise ProcessShutdownError("subprocess I/O failed")
            return BoundedProcessResult(
                returncode=process.returncode,
                stdout=bytes(stdout_buffer),
                stderr=bytes(stderr_buffer),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except BaseException:
            if process is not None:
                terminate_process_group(process, self._config.terminate_grace_seconds)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BaseException:
                            pass
                try:
                    process.wait(timeout=self._config.terminate_grace_seconds)
                except BaseException:
                    pass
            for thread in threads:
                thread.join(timeout=self._config.terminate_grace_seconds)
            raise
        finally:
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BaseException:
                            pass


__all__ = [
    "BoundedProcessConfig",
    "BoundedProcessResult",
    "BoundedProcessRunner",
    "ProcessOutputOverflow",
    "ProcessShutdownError",
    "ProcessTimeoutError",
    "terminate_process_group",
]

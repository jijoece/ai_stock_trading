#!/usr/bin/env python3
"""Minimal Anthropic Message Batches skeleton for milestone patch generation.

This is a patch-generation workflow, not an autonomous coding agent.
See docs/batch_creation.md for the full specification.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON_SYSTEM_PATH = REPO_ROOT / "batch" / "common_system.md"

CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
DEFAULT_CONTEXT_LIMIT_BYTES = 2 * 1024 * 1024

ALLOWED_MANIFEST_KEYS = {"model", "max_tokens", "temperature", "requests", "context_limit_bytes"}
ALLOWED_REQUEST_KEYS = {"custom_id", "prompt_file", "context_files", "max_tokens", "temperature"}


class ManifestError(ValueError):
    """Raised for any local manifest validation failure."""


@dataclass
class RequestSpec:
    custom_id: str
    prompt_file: Path
    context_files: list[Path]
    model: str
    max_tokens: int
    temperature: float
    prompt_text: str
    context_bytes: int


def _resolve_repo_path(raw: str, repo_root: Path) -> Path:
    candidate = (repo_root / raw).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        raise ManifestError(f"path escapes repository root: {raw}")
    return candidate


def load_manifest(
    manifest_path: Path,
    repo_root: Path = REPO_ROOT,
) -> tuple[list[RequestSpec], dict[str, Any]]:
    """Load and fully validate a manifest. Raises ManifestError on any problem."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")

    unknown = set(raw.keys()) - ALLOWED_MANIFEST_KEYS
    if unknown:
        raise ManifestError(f"unknown manifest keys: {sorted(unknown)}")

    manifest_model = raw.get("model")
    if manifest_model == "MODEL_FROM_ENV":
        manifest_model = None
    env_model = os.environ.get("ANTHROPIC_MODEL") or None
    model = manifest_model or env_model
    if not model:
        raise ManifestError(
            "no model resolved: set 'model' in the manifest or ANTHROPIC_MODEL"
        )

    default_max_tokens = raw.get("max_tokens")
    if default_max_tokens is None:
        raise ManifestError("manifest must set a default 'max_tokens'")
    if not isinstance(default_max_tokens, int) or default_max_tokens < 1:
        raise ManifestError("'max_tokens' must be an integer >= 1")

    default_temperature = raw.get("temperature", 0)
    if not isinstance(default_temperature, (int, float)):
        raise ManifestError("'temperature' must be a number")

    context_limit_bytes = raw.get("context_limit_bytes", DEFAULT_CONTEXT_LIMIT_BYTES)
    if not isinstance(context_limit_bytes, int) or context_limit_bytes < 1:
        raise ManifestError("'context_limit_bytes' must be an integer >= 1")

    requests_raw = raw.get("requests")
    if not isinstance(requests_raw, list) or not requests_raw:
        raise ManifestError("manifest must contain a non-empty 'requests' list")

    resolved_repo_root = repo_root.resolve()
    specs: list[RequestSpec] = []
    seen_ids: set[str] = set()

    for i, req in enumerate(requests_raw):
        if not isinstance(req, dict):
            raise ManifestError(f"requests[{i}] must be an object")

        unknown_req = set(req.keys()) - ALLOWED_REQUEST_KEYS
        if unknown_req:
            raise ManifestError(f"requests[{i}] has unknown keys: {sorted(unknown_req)}")

        custom_id = req.get("custom_id")
        if not isinstance(custom_id, str) or not CUSTOM_ID_RE.match(custom_id):
            raise ManifestError(
                f"requests[{i}] custom_id is invalid: {custom_id!r} "
                f"(must match {CUSTOM_ID_RE.pattern})"
            )
        if custom_id in seen_ids:
            raise ManifestError(f"duplicate custom_id: {custom_id}")
        seen_ids.add(custom_id)

        prompt_file_raw = req.get("prompt_file")
        if not isinstance(prompt_file_raw, str) or not prompt_file_raw:
            raise ManifestError(f"requests[{i}] ({custom_id}) missing 'prompt_file'")
        prompt_path = _resolve_repo_path(prompt_file_raw, resolved_repo_root)
        if not prompt_path.is_file():
            raise ManifestError(f"requests[{i}] ({custom_id}) prompt file not found: {prompt_file_raw}")

        context_files_raw = req.get("context_files", [])
        if not isinstance(context_files_raw, list):
            raise ManifestError(f"requests[{i}] ({custom_id}) 'context_files' must be a list")

        context_paths: list[Path] = []
        for cf in context_files_raw:
            if not isinstance(cf, str) or not cf:
                raise ManifestError(f"requests[{i}] ({custom_id}) invalid context file entry: {cf!r}")
            cf_path = _resolve_repo_path(cf, resolved_repo_root)
            if not cf_path.exists():
                raise ManifestError(f"requests[{i}] ({custom_id}) context file not found: {cf}")
            if cf_path.is_dir():
                raise ManifestError(f"requests[{i}] ({custom_id}) context path is a directory: {cf}")
            context_paths.append(cf_path)

        req_max_tokens = req.get("max_tokens", default_max_tokens)
        if not isinstance(req_max_tokens, int) or req_max_tokens < 1:
            raise ManifestError(f"requests[{i}] ({custom_id}) 'max_tokens' must be an integer >= 1")

        req_temperature = req.get("temperature", default_temperature)
        if not isinstance(req_temperature, (int, float)):
            raise ManifestError(f"requests[{i}] ({custom_id}) 'temperature' must be a number")

        prompt_text = prompt_path.read_text(encoding="utf-8")
        context_bytes = prompt_path.stat().st_size
        for cf_path in context_paths:
            context_bytes += cf_path.stat().st_size

        if context_bytes > context_limit_bytes:
            raise ManifestError(
                f"requests[{i}] ({custom_id}) exceeds local context limit: "
                f"{context_bytes} > {context_limit_bytes} bytes"
            )

        specs.append(
            RequestSpec(
                custom_id=custom_id,
                prompt_file=prompt_path,
                context_files=context_paths,
                model=model,
                max_tokens=req_max_tokens,
                temperature=req_temperature,
                prompt_text=prompt_text,
                context_bytes=context_bytes,
            )
        )

    summary = {
        "requests": len(specs),
        "model": model,
        "total_context_bytes": sum(s.context_bytes for s in specs),
        "largest_request_bytes": max((s.context_bytes for s in specs), default=0),
    }
    return specs, summary


def _relative_path(p: Path, repo_root: Path = REPO_ROOT) -> str:
    return str(p.resolve().relative_to(repo_root.resolve()))


def build_user_message(spec: RequestSpec, repo_root: Path = REPO_ROOT) -> str:
    """Construct the per-request user message content, safely delimiting file content."""
    context_blocks = []
    for cf in spec.context_files:
        rel = _relative_path(cf, repo_root)
        contents = cf.read_text(encoding="utf-8", errors="replace")
        context_blocks.append(f'<FILE path="{rel}">\n{contents}\n</FILE>')

    context_section = "\n\n".join(context_blocks)

    return (
        "<MILESTONE_PROMPT>\n"
        f"{spec.prompt_text}\n"
        "</MILESTONE_PROMPT>\n"
        "\n"
        "<REPOSITORY_CONTEXT>\n"
        f"{context_section}\n"
        "</REPOSITORY_CONTEXT>\n"
        "\n"
        "<OUTPUT_REQUIREMENTS>\n"
        "Return a unified diff against the supplied repository snapshot.\n"
        "\n"
        "Include new files using normal /dev/null unified-diff syntax.\n"
        "\n"
        "Do not include markdown fences around the patch.\n"
        "\n"
        "Do not repeat complete source files outside the patch.\n"
        "\n"
        "Do not claim tests were executed.\n"
        "</OUTPUT_REQUIREMENTS>"
    )


def build_system_blocks(supports_cache_control: bool) -> Any:
    text = COMMON_SYSTEM_PATH.read_text(encoding="utf-8")
    if supports_cache_control:
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    return text


def _sdk_supports_cache_control() -> bool:
    try:
        from anthropic.types.text_block_param import TextBlockParam
    except ImportError:
        return False
    return "cache_control" in TextBlockParam.__annotations__


def build_batch_requests(specs: list[RequestSpec], repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    system_blocks = build_system_blocks(_sdk_supports_cache_control())
    batch_requests = []
    for spec in specs:
        user_content = build_user_message(spec, repo_root)
        batch_requests.append(
            {
                "custom_id": spec.custom_id,
                "params": {
                    "model": spec.model,
                    "max_tokens": spec.max_tokens,
                    "temperature": spec.temperature,
                    "system": system_blocks,
                    "messages": [{"role": "user", "content": user_content}],
                },
            }
        )
    return batch_requests


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    try:
        _specs, summary = load_manifest(manifest_path, repo_root=REPO_ROOT)
    except ManifestError as e:
        print(f"validate: FAILED: {e}", file=sys.stderr)
        return 1

    print(f"requests: {summary['requests']}")
    print(f"model: {summary['model']}")
    print(f"total context bytes: {summary['total_context_bytes']}")
    print(f"largest request bytes: {summary['largest_request_bytes']}")
    print("valid: true")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    try:
        specs, _summary = load_manifest(manifest_path, repo_root=REPO_ROOT)
    except ManifestError as e:
        print(f"submit: FAILED: {e}", file=sys.stderr)
        return 1

    import anthropic

    client = anthropic.Anthropic()
    batch_requests = build_batch_requests(specs, repo_root=REPO_ROOT)

    batch = client.messages.batches.create(requests=batch_requests)  # type: ignore[arg-type]

    run_dir = REPO_ROOT / "batch" / "runs" / batch.id
    run_dir.mkdir(parents=True, exist_ok=True)

    batch_json = _sanitized_batch_metadata(batch)
    with open(run_dir / "batch.json", "w", encoding="utf-8") as f:
        json.dump(batch_json, f, indent=2)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_snapshot = json.load(f)
    with open(run_dir / "manifest.snapshot.json", "w", encoding="utf-8") as f:
        json.dump(manifest_snapshot, f, indent=2)

    request_index = {
        spec.custom_id: {
            "prompt_file": _relative_path(spec.prompt_file, REPO_ROOT),
            "context_files": [_relative_path(p, REPO_ROOT) for p in spec.context_files],
            "max_tokens": spec.max_tokens,
        }
        for spec in specs
    }
    with open(run_dir / "request-index.json", "w", encoding="utf-8") as f:
        json.dump(request_index, f, indent=2)

    print(f"batch_id: {batch.id}")
    print(f"processing_status: {batch.processing_status}")
    print(f"request count: {len(specs)}")
    print(f"run directory: {run_dir}")
    return 0


def _sanitized_batch_metadata(batch: Any) -> dict[str, Any]:
    def _iso(v: Any) -> Any:
        return v.isoformat() if v is not None and hasattr(v, "isoformat") else v

    return {
        "batch_id": batch.id,
        "processing_status": batch.processing_status,
        "created_at": _iso(batch.created_at),
        "ended_at": _iso(getattr(batch, "ended_at", None)),
        "expires_at": _iso(batch.expires_at),
        "cancel_initiated_at": _iso(getattr(batch, "cancel_initiated_at", None)),
        "request_counts": dict(batch.request_counts) if hasattr(batch.request_counts, "keys")
        else {
            "processing": batch.request_counts.processing,
            "succeeded": batch.request_counts.succeeded,
            "errored": batch.request_counts.errored,
            "canceled": batch.request_counts.canceled,
            "expired": batch.request_counts.expired,
        },
    }


def cmd_status(args: argparse.Namespace) -> int:
    import anthropic

    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(args.batch_id)

    sanitized = _sanitized_batch_metadata(batch)
    print(json.dumps(sanitized, indent=2))

    run_dir = REPO_ROOT / "batch" / "runs" / args.batch_id
    if run_dir.is_dir():
        with open(run_dir / "batch.json", "w", encoding="utf-8") as f:
            json.dump(sanitized, f, indent=2)
    return 0


def _extract_tagged(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def cmd_fetch(args: argparse.Namespace) -> int:
    import anthropic

    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(args.batch_id)

    if batch.processing_status != "ended":
        print(
            f"fetch: batch {args.batch_id} is not ended yet "
            f"(processing_status={batch.processing_status})",
            file=sys.stderr,
        )
        return 1

    run_dir = REPO_ROOT / "batch" / "runs" / args.batch_id
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {}

    for item in client.messages.batches.results(args.batch_id):
        custom_id = item.custom_id
        result_dir = results_dir / custom_id
        result_dir.mkdir(parents=True, exist_ok=True)

        result = item.result
        result_type = result.type

        response_text = ""
        input_tokens = None
        output_tokens = None
        error_type = None
        result_json: dict[str, Any] = {"custom_id": custom_id, "status": result_type}

        if result_type == "succeeded":
            message = result.message  # type: ignore[union-attr]
            for block in message.content:
                block_text = getattr(block, "text", None)
                if isinstance(block_text, str):
                    response_text += block_text
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            result_json["usage"] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            result_json["stop_reason"] = message.stop_reason
        elif result_type == "errored":
            error = result.error.error  # type: ignore[union-attr]
            error_type = getattr(error, "type", None)
            result_json["error"] = {
                "type": error_type,
                "message": getattr(error, "message", None),
            }
        elif result_type in ("canceled", "expired"):
            pass

        with open(result_dir / "result.json", "w", encoding="utf-8") as f:
            json.dump(result_json, f, indent=2)
        with open(result_dir / "response.txt", "w", encoding="utf-8") as f:
            f.write(response_text)

        patch_text = _extract_tagged(response_text, "patch")
        notes_text = _extract_tagged(response_text, "notes")
        needs_context_text = _extract_tagged(response_text, "needs_context")

        with open(result_dir / "implementation.patch", "w", encoding="utf-8") as f:
            f.write(patch_text)
        with open(result_dir / "notes.txt", "w", encoding="utf-8") as f:
            f.write(notes_text)
        with open(result_dir / "needs_context.txt", "w", encoding="utf-8") as f:
            f.write(needs_context_text)

        summary[custom_id] = {
            "status": result_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "patch_present": bool(patch_text.strip()),
            "needs_context_present": bool(needs_context_text.strip()),
            "error_type": error_type,
        }

    with open(run_dir / "results-summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"fetch: wrote {len(summary)} result(s) to {results_dir}")
    return 0


def _run_git_apply_check(patch_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def cmd_check_patch(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    patch_path = run_dir / "results" / args.custom_id / "implementation.patch"

    if not patch_path.is_file():
        print(f"check-patch: FAILED: patch not found: {patch_path}", file=sys.stderr)
        return 1

    if patch_path.stat().st_size == 0:
        print(f"check-patch: FAILED: patch is empty: {patch_path}", file=sys.stderr)
        return 1

    result = _run_git_apply_check(patch_path)
    if result.returncode == 0:
        print(f"check-patch: OK: {args.custom_id}")
        return 0

    print(f"check-patch: FAILED: {args.custom_id}", file=sys.stderr)
    print(result.stderr.strip(), file=sys.stderr)
    return 1


def _is_inside_git_worktree() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _has_dirty_tracked_changes() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def cmd_apply_patch(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    patch_path = run_dir / "results" / args.custom_id / "implementation.patch"

    if not patch_path.is_file():
        print(f"apply-patch: FAILED: patch not found: {patch_path}", file=sys.stderr)
        return 1

    if patch_path.stat().st_size == 0:
        print(f"apply-patch: FAILED: patch is empty: {patch_path}", file=sys.stderr)
        return 1

    if not _is_inside_git_worktree():
        print("apply-patch: FAILED: not inside a Git worktree", file=sys.stderr)
        return 1

    if _has_dirty_tracked_changes() and not args.allow_dirty:
        print(
            "apply-patch: FAILED: uncommitted tracked-file changes present "
            "(pass --allow-dirty to override)",
            file=sys.stderr,
        )
        return 1

    check_result = _run_git_apply_check(patch_path)
    if check_result.returncode != 0:
        print(f"apply-patch: FAILED: git apply --check failed for {args.custom_id}", file=sys.stderr)
        print(check_result.stderr.strip(), file=sys.stderr)
        return 1

    apply_result = subprocess.run(
        ["git", "apply", str(patch_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if apply_result.returncode != 0:
        print(f"apply-patch: FAILED: git apply failed for {args.custom_id}", file=sys.stderr)
        print(apply_result.stderr.strip(), file=sys.stderr)
        return 1

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    print(f"apply-patch: applied {args.custom_id}")
    print(status_result.stdout.strip())
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Anthropic Message Batches milestone patch skeleton")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate a manifest locally, no API calls")
    p_validate.add_argument("--manifest", required=True)
    p_validate.set_defaults(func=cmd_validate)

    p_submit = sub.add_parser("submit", help="Submit a Message Batch")
    p_submit.add_argument("--manifest", required=True)
    p_submit.set_defaults(func=cmd_submit)

    p_status = sub.add_parser("status", help="Retrieve batch status")
    p_status.add_argument("--batch-id", required=True)
    p_status.set_defaults(func=cmd_status)

    p_fetch = sub.add_parser("fetch", help="Fetch and save batch results")
    p_fetch.add_argument("--batch-id", required=True)
    p_fetch.set_defaults(func=cmd_fetch)

    p_check = sub.add_parser("check-patch", help="Validate a patch with git apply --check")
    p_check.add_argument("--run-dir", required=True)
    p_check.add_argument("--custom-id", required=True)
    p_check.set_defaults(func=cmd_check_patch)

    p_apply = sub.add_parser("apply-patch", help="Apply a single patch to the working tree")
    p_apply.add_argument("--run-dir", required=True)
    p_apply.add_argument("--custom-id", required=True)
    p_apply.add_argument("--allow-dirty", action="store_true")
    p_apply.set_defaults(func=cmd_apply_patch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

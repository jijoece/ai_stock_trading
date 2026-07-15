"""Offline unit tests for scripts/milestone_batch.py.

No real Anthropic API requests are made. The `anthropic` client is faked via
lightweight stand-in objects.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "milestone_batch.py"
_spec = importlib.util.spec_from_file_location("milestone_batch", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
mb = importlib.util.module_from_spec(_spec)
sys.modules["milestone_batch"] = mb
_spec.loader.exec_module(mb)  # type: ignore[union-attr]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "batch" / "common_system.md", "shared system text")
    _write(repo / "batch" / "prompts" / "m1.md", "Milestone 1 prompt.")
    _write(repo / "batch" / "prompts" / "m2.md", "Milestone 2 prompt.")
    _write(repo / "src" / "pkg" / "a.py", "print('a')\n")
    _write(repo / "src" / "pkg" / "b.py", "print('b')\n")
    return repo


def _manifest(**overrides):
    base = {
        "model": "claude-test-model",
        "max_tokens": 1000,
        "temperature": 0,
        "requests": [
            {
                "custom_id": "milestone-1",
                "prompt_file": "batch/prompts/m1.md",
                "context_files": ["src/pkg/a.py"],
            }
        ],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 1. valid manifest
# --------------------------------------------------------------------------


def test_valid_manifest_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    repo = _make_repo(tmp_path)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(_manifest()))

    specs, summary = mb.load_manifest(manifest_path, repo_root=repo)

    assert summary["requests"] == 1
    assert summary["model"] == "claude-test-model"
    assert specs[0].custom_id == "milestone-1"


# --------------------------------------------------------------------------
# 2 & 3. invalid / duplicate custom IDs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "has a space", "x" * 65, "bad/slash"])
def test_invalid_custom_id_rejected(tmp_path, bad_id):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    manifest["requests"][0]["custom_id"] = bad_id
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="custom_id"):
        mb.load_manifest(manifest_path, repo_root=repo)


def test_duplicate_custom_id_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    dup = dict(manifest["requests"][0])
    dup["context_files"] = ["src/pkg/b.py"]
    manifest["requests"].append(dup)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="duplicate custom_id"):
        mb.load_manifest(manifest_path, repo_root=repo)


# --------------------------------------------------------------------------
# 3. path traversal rejection
# --------------------------------------------------------------------------


def test_path_traversal_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n")

    manifest = _manifest()
    manifest["requests"][0]["context_files"] = ["../outside.py"]
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="escapes repository root"):
        mb.load_manifest(manifest_path, repo_root=repo)


# --------------------------------------------------------------------------
# 4. missing prompt/context file
# --------------------------------------------------------------------------


def test_missing_prompt_file_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    manifest["requests"][0]["prompt_file"] = "batch/prompts/missing.md"
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="prompt file not found"):
        mb.load_manifest(manifest_path, repo_root=repo)


def test_missing_context_file_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    manifest["requests"][0]["context_files"] = ["src/pkg/missing.py"]
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="context file not found"):
        mb.load_manifest(manifest_path, repo_root=repo)


def test_directory_context_file_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    manifest["requests"][0]["context_files"] = ["src/pkg"]
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="directory"):
        mb.load_manifest(manifest_path, repo_root=repo)


# --------------------------------------------------------------------------
# 5. per-request context limit
# --------------------------------------------------------------------------


def test_context_limit_enforced(tmp_path):
    repo = _make_repo(tmp_path)
    manifest = _manifest(context_limit_bytes=10)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(mb.ManifestError, match="exceeds local context limit"):
        mb.load_manifest(manifest_path, repo_root=repo)


# --------------------------------------------------------------------------
# 6. request construction
# --------------------------------------------------------------------------


def test_build_user_message_contains_sections(tmp_path):
    repo = _make_repo(tmp_path)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(_manifest()))
    specs, _ = mb.load_manifest(manifest_path, repo_root=repo)

    message = mb.build_user_message(specs[0], repo_root=repo)

    assert "<MILESTONE_PROMPT>" in message
    assert "Milestone 1 prompt." in message
    assert '<FILE path="src/pkg/a.py">' in message
    assert "print('a')" in message
    assert "<OUTPUT_REQUIREMENTS>" in message


# --------------------------------------------------------------------------
# 7. shared system content is identical across requests
# --------------------------------------------------------------------------


def test_shared_system_identical_across_requests(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    manifest = _manifest()
    manifest["requests"].append(
        {
            "custom_id": "milestone-2",
            "prompt_file": "batch/prompts/m2.md",
            "context_files": ["src/pkg/b.py"],
        }
    )
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(manifest))
    specs, _ = mb.load_manifest(manifest_path, repo_root=repo)

    monkeypatch.setattr(mb, "COMMON_SYSTEM_PATH", repo / "batch" / "common_system.md")
    batch_requests = mb.build_batch_requests(specs, repo_root=repo)

    systems = [r["params"]["system"] for r in batch_requests]
    assert systems[0] == systems[1]
    assert len(batch_requests) == 2
    assert {r["custom_id"] for r in batch_requests} == {"milestone-1", "milestone-2"}


# --------------------------------------------------------------------------
# 8. submit metadata persistence (no API key, sanitized batch.json)
# --------------------------------------------------------------------------


class _FakeRequestCounts:
    def __init__(self):
        self.processing = 1
        self.succeeded = 0
        self.errored = 0
        self.canceled = 0
        self.expired = 0


class _FakeBatch:
    def __init__(self, batch_id="batch_123", processing_status="in_progress"):
        self.id = batch_id
        self.processing_status = processing_status
        self.created_at = SimpleNamespace(isoformat=lambda: "2026-07-14T00:00:00Z")
        self.ended_at = None
        self.expires_at = SimpleNamespace(isoformat=lambda: "2026-07-15T00:00:00Z")
        self.cancel_initiated_at = None
        self.request_counts = _FakeRequestCounts()
        self.results_url = None


class _FakeBatchesResource:
    def __init__(self, batch):
        self._batch = batch
        self.created_requests = None

    def create(self, requests):
        self.created_requests = list(requests)
        return self._batch

    def retrieve(self, batch_id):
        return self._batch

    def results(self, batch_id):
        return iter(())


class _FakeMessagesResource:
    def __init__(self, batch):
        self.batches = _FakeBatchesResource(batch)


class _FakeAnthropicClient:
    def __init__(self, batch=None, *args, **kwargs):
        self.messages = _FakeMessagesResource(batch or _FakeBatch())


def test_submit_persists_sanitized_metadata_no_api_key(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(_manifest()))
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    monkeypatch.setattr(mb, "COMMON_SYSTEM_PATH", repo / "batch" / "common_system.md")

    fake_batch = _FakeBatch(batch_id="batch_abc")
    fake_client = _FakeAnthropicClient(batch=fake_batch)

    fake_anthropic_module = SimpleNamespace(Anthropic=lambda: fake_client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    args = SimpleNamespace(manifest=str(manifest_path))
    rc = mb.cmd_submit(args)
    assert rc == 0

    run_dir = repo / "batch" / "runs" / "batch_abc"
    batch_json = json.loads((run_dir / "batch.json").read_text())
    assert batch_json["batch_id"] == "batch_abc"
    assert batch_json["processing_status"] == "in_progress"

    request_index = json.loads((run_dir / "request-index.json").read_text())
    assert request_index["milestone-1"]["prompt_file"] == "batch/prompts/m1.md"

    for saved in run_dir.rglob("*"):
        if saved.is_file():
            text = saved.read_text(encoding="utf-8", errors="ignore")
            assert "ANTHROPIC_API_KEY" not in text
            assert "sk-ant-" not in text

    assert fake_client.messages.batches.created_requests is not None
    assert len(fake_client.messages.batches.created_requests) == 1
    assert fake_client.messages.batches.created_requests[0]["custom_id"] == "milestone-1"


# --------------------------------------------------------------------------
# 9. out-of-order results map correctly by custom_id
# --------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text, input_tokens=10, output_tokens=20, stop_reason="end_turn"):
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)
        self.stop_reason = stop_reason


class _FakeSucceededResult:
    type = "succeeded"

    def __init__(self, text):
        self.message = _FakeMessage(text)


class _FakeErrorInner:
    def __init__(self, error_type, message):
        self.type = error_type
        self.message = message


class _FakeErrorWrapper:
    def __init__(self, error_type, message):
        self.error = _FakeErrorInner(error_type, message)


class _FakeErroredResult:
    type = "errored"

    def __init__(self, error_type="invalid_request_error", message="bad request"):
        self.error = _FakeErrorWrapper(error_type, message)


class _FakeCanceledResult:
    type = "canceled"


class _FakeExpiredResult:
    type = "expired"


class _FakeIndividualResponse:
    def __init__(self, custom_id, result):
        self.custom_id = custom_id
        self.result = result


def _patch(text, notes="notes here", needs_context=""):
    body = f"<patch>\n{text}\n</patch>\n<notes>\n{notes}\n</notes>"
    if needs_context:
        body += f"\n<needs_context>\n{needs_context}\n</needs_context>"
    return body


def test_fetch_maps_out_of_order_results_by_custom_id(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    results = [
        _FakeIndividualResponse("milestone-b", _FakeSucceededResult(_patch("diff --git a/b b/b"))),
        _FakeIndividualResponse("milestone-a", _FakeSucceededResult(_patch("diff --git a/a a/a"))),
    ]

    batch = _FakeBatch(batch_id="batch_xyz", processing_status="ended")

    class _ResultsBatchesResource(_FakeBatchesResource):
        def results(self, batch_id):
            return iter(results)

    fake_client = _FakeAnthropicClient(batch=batch)
    fake_client.messages.batches = _ResultsBatchesResource(batch)
    fake_anthropic_module = SimpleNamespace(Anthropic=lambda: fake_client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    args = SimpleNamespace(batch_id="batch_xyz")
    rc = mb.cmd_fetch(args)
    assert rc == 0

    run_dir = repo / "batch" / "runs" / "batch_xyz"
    patch_a = (run_dir / "results" / "milestone-a" / "implementation.patch").read_text()
    patch_b = (run_dir / "results" / "milestone-b" / "implementation.patch").read_text()
    assert "diff --git a/a a/a" in patch_a
    assert "diff --git a/b b/b" in patch_b


# --------------------------------------------------------------------------
# 10. successful patch extraction
# --------------------------------------------------------------------------


def test_successful_patch_extraction(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    patch_text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    results = [
        _FakeIndividualResponse(
            "milestone-1", _FakeSucceededResult(_patch(patch_text, notes="looks good", needs_context="path/missing.py"))
        )
    ]
    batch = _FakeBatch(batch_id="batch_ok", processing_status="ended")

    class _ResultsBatchesResource(_FakeBatchesResource):
        def results(self, batch_id):
            return iter(results)

    fake_client = _FakeAnthropicClient(batch=batch)
    fake_client.messages.batches = _ResultsBatchesResource(batch)
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: fake_client))

    rc = mb.cmd_fetch(SimpleNamespace(batch_id="batch_ok"))
    assert rc == 0

    result_dir = repo / "batch" / "runs" / "batch_ok" / "results" / "milestone-1"
    assert patch_text.strip() in (result_dir / "implementation.patch").read_text()
    assert "looks good" in (result_dir / "notes.txt").read_text()
    assert "path/missing.py" in (result_dir / "needs_context.txt").read_text()

    summary = json.loads((repo / "batch" / "runs" / "batch_ok" / "results-summary.json").read_text())
    assert summary["milestone-1"]["status"] == "succeeded"
    assert summary["milestone-1"]["patch_present"] is True
    assert summary["milestone-1"]["needs_context_present"] is True


# --------------------------------------------------------------------------
# 11. malformed response handling
# --------------------------------------------------------------------------


def test_malformed_response_missing_tags_handled(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    results = [_FakeIndividualResponse("milestone-1", _FakeSucceededResult("no tags at all here"))]
    batch = _FakeBatch(batch_id="batch_malformed", processing_status="ended")

    class _ResultsBatchesResource(_FakeBatchesResource):
        def results(self, batch_id):
            return iter(results)

    fake_client = _FakeAnthropicClient(batch=batch)
    fake_client.messages.batches = _ResultsBatchesResource(batch)
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: fake_client))

    rc = mb.cmd_fetch(SimpleNamespace(batch_id="batch_malformed"))
    assert rc == 0

    result_dir = repo / "batch" / "runs" / "batch_malformed" / "results" / "milestone-1"
    assert (result_dir / "implementation.patch").read_text() == ""
    assert (result_dir / "notes.txt").read_text() == ""
    assert (result_dir / "needs_context.txt").read_text() == ""


# --------------------------------------------------------------------------
# 12. errored/canceled/expired result handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result_obj,expected_status",
    [
        (_FakeErroredResult(), "errored"),
        (_FakeCanceledResult(), "canceled"),
        (_FakeExpiredResult(), "expired"),
    ],
)
def test_non_succeeded_results_do_not_create_fake_patch(tmp_path, monkeypatch, result_obj, expected_status):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    results = [_FakeIndividualResponse("milestone-1", result_obj)]
    batch = _FakeBatch(batch_id=f"batch_{expected_status}", processing_status="ended")

    class _ResultsBatchesResource(_FakeBatchesResource):
        def results(self, batch_id):
            return iter(results)

    fake_client = _FakeAnthropicClient(batch=batch)
    fake_client.messages.batches = _ResultsBatchesResource(batch)
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: fake_client))

    rc = mb.cmd_fetch(SimpleNamespace(batch_id=f"batch_{expected_status}"))
    assert rc == 0

    result_dir = repo / "batch" / "runs" / f"batch_{expected_status}" / "results" / "milestone-1"
    assert (result_dir / "implementation.patch").read_text() == ""

    summary = json.loads(
        (repo / "batch" / "runs" / f"batch_{expected_status}" / "results-summary.json").read_text()
    )
    assert summary["milestone-1"]["status"] == expected_status
    assert summary["milestone-1"]["patch_present"] is False


# --------------------------------------------------------------------------
# 13. no result fetch before ended
# --------------------------------------------------------------------------


def test_fetch_refuses_before_ended(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    batch = _FakeBatch(batch_id="batch_pending", processing_status="in_progress")
    fake_client = _FakeAnthropicClient(batch=batch)
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: fake_client))

    rc = mb.cmd_fetch(SimpleNamespace(batch_id="batch_pending"))
    assert rc != 0
    assert not (repo / "batch" / "runs" / "batch_pending" / "results").exists()


# --------------------------------------------------------------------------
# 14. git apply --check command construction
# --------------------------------------------------------------------------


def test_check_patch_invokes_git_apply_check(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)

    run_dir = repo / "batch" / "runs" / "batch_check"
    patch_dir = run_dir / "results" / "milestone-1"
    _write(patch_dir / "implementation.patch", "diff --git a/x b/x\n")

    captured = {}

    def _fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mb.subprocess, "run", _fake_run)

    rc = mb.cmd_check_patch(SimpleNamespace(run_dir=str(run_dir), custom_id="milestone-1"))
    assert rc == 0
    assert captured["cmd"][:3] == ["git", "apply", "--check"]
    assert captured["cmd"][-1] == str(patch_dir / "implementation.patch")
    assert captured["cwd"] == repo


def test_check_patch_empty_patch_fails_without_subprocess(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    run_dir = repo / "batch" / "runs" / "batch_empty"
    _write(run_dir / "results" / "milestone-1" / "implementation.patch", "")

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for an empty patch")

    monkeypatch.setattr(mb.subprocess, "run", _boom)

    rc = mb.cmd_check_patch(SimpleNamespace(run_dir=str(run_dir), custom_id="milestone-1"))
    assert rc != 0


# --------------------------------------------------------------------------
# 15. API key never appears in saved files
# --------------------------------------------------------------------------


def test_api_key_never_persisted(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    manifest_path = repo / "batch" / "manifest.json"
    _write(manifest_path, json.dumps(_manifest()))
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    monkeypatch.setattr(mb, "COMMON_SYSTEM_PATH", repo / "batch" / "common_system.md")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")

    fake_batch = _FakeBatch(batch_id="batch_secret")
    fake_client = _FakeAnthropicClient(batch=fake_batch)
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: fake_client))

    rc = mb.cmd_submit(SimpleNamespace(manifest=str(manifest_path)))
    assert rc == 0

    run_dir = repo / "batch" / "runs" / "batch_secret"
    for saved in run_dir.rglob("*"):
        if saved.is_file():
            assert "sk-ant-super-secret-value" not in saved.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------
# apply-patch safety gates
# --------------------------------------------------------------------------


def test_apply_patch_refuses_empty_patch(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    run_dir = repo / "batch" / "runs" / "batch_x"
    _write(run_dir / "results" / "milestone-1" / "implementation.patch", "")

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for an empty patch")

    monkeypatch.setattr(mb.subprocess, "run", _boom)

    rc = mb.cmd_apply_patch(
        SimpleNamespace(run_dir=str(run_dir), custom_id="milestone-1", allow_dirty=False)
    )
    assert rc != 0


def test_apply_patch_refuses_dirty_tree_without_flag(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    run_dir = repo / "batch" / "runs" / "batch_y"
    _write(run_dir / "results" / "milestone-1" / "implementation.patch", "diff --git a/x b/x\n")

    monkeypatch.setattr(mb, "_is_inside_git_worktree", lambda: True)
    monkeypatch.setattr(mb, "_has_dirty_tracked_changes", lambda: True)

    def _boom(*args, **kwargs):
        raise AssertionError("git apply --check should not run when dirty and not allowed")

    monkeypatch.setattr(mb, "_run_git_apply_check", _boom)

    rc = mb.cmd_apply_patch(
        SimpleNamespace(run_dir=str(run_dir), custom_id="milestone-1", allow_dirty=False)
    )
    assert rc != 0


def test_apply_patch_runs_check_before_apply(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(mb, "REPO_ROOT", repo)
    run_dir = repo / "batch" / "runs" / "batch_z"
    _write(run_dir / "results" / "milestone-1" / "implementation.patch", "diff --git a/x b/x\n")

    monkeypatch.setattr(mb, "_is_inside_git_worktree", lambda: True)
    monkeypatch.setattr(mb, "_has_dirty_tracked_changes", lambda: False)
    monkeypatch.setattr(mb, "_run_git_apply_check", lambda p: SimpleNamespace(returncode=0, stdout="", stderr=""))

    calls = []

    def _fake_run(cmd, cwd, capture_output, text):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="M file.py\n", stderr="")

    monkeypatch.setattr(mb.subprocess, "run", _fake_run)

    rc = mb.cmd_apply_patch(
        SimpleNamespace(run_dir=str(run_dir), custom_id="milestone-1", allow_dirty=False)
    )
    assert rc == 0
    assert calls[0][:2] == ["git", "apply"]
    assert calls[0][-1].endswith("implementation.patch")

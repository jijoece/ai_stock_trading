"""Prompt registry and versioning tests (docs/milestone-5.md Step 11; per-role default
version selection added as a Milestone 6.1 follow-up)."""
from __future__ import annotations

import pytest

from trading_research.research.prompt_registry import DEFAULT_ROLE_PROMPT_VERSIONS, PromptNotFoundError, PromptRegistry


def test_registry_loads_shipped_role_prompts():
    registry = PromptRegistry()
    for role in ("fundamental", "technical", "bull", "manager"):
        prompt = registry.get(role)
        assert prompt.role == role
        assert prompt.version == "v1"
        assert prompt.text_hash
        assert len(prompt.text) > 0


def test_bear_role_defaults_to_v2():
    """Milestone 6.1 follow-up: the hardened bear prompt is the default a caller gets
    when it omits `version` — matching every other real call site in orchestration.py
    (`prompt_registry.get(role)`, no explicit version)."""
    registry = PromptRegistry()
    prompt = registry.get("bear")
    assert prompt.version == "v2"
    assert prompt.role == "bear"
    assert "never invent a downside percentage" in prompt.text


def test_bear_v1_still_loadable_explicitly_and_unchanged():
    """v1 is preserved, not deleted — a caller that explicitly asks for v1 still gets the
    original Milestone 5 prompt text, byte-for-byte."""
    registry = PromptRegistry()
    prompt = registry.get("bear", version="v1")
    assert prompt.version == "v1"
    assert "never invent a downside percentage" not in prompt.text
    assert "You have no ability to size a position" in prompt.text


def test_bear_v1_and_v2_have_different_hashes():
    registry = PromptRegistry()
    v1 = registry.get("bear", version="v1")
    v2 = registry.get("bear", version="v2")
    assert v1.text_hash != v2.text_hash


def test_default_role_prompt_versions_only_overrides_bear():
    assert DEFAULT_ROLE_PROMPT_VERSIONS == {"bear": "v2"}
    registry = PromptRegistry()
    for role in ("fundamental", "technical", "bull", "manager"):
        assert registry.get(role).version == "v1"


def test_role_versions_override_is_isolated_per_registry_instance():
    """A caller-supplied override dict must not leak into the shared class-level default
    or into a different `PromptRegistry` instance."""
    custom_registry = PromptRegistry(role_versions={"bear": "v1"})
    default_registry = PromptRegistry()
    assert custom_registry.get("bear").version == "v1"
    assert default_registry.get("bear").version == "v2"


def test_missing_prompt_file_raises():
    registry = PromptRegistry()
    with pytest.raises(PromptNotFoundError):
        registry.get("nonexistent-role")


def test_editing_prompt_text_changes_hash_even_with_same_version(tmp_path):
    root = tmp_path / "prompts"
    (root / "fundamental").mkdir(parents=True)
    prompt_file = root / "fundamental" / "v1.txt"
    prompt_file.write_text("Original text.")
    registry = PromptRegistry(root)
    original_hash = registry.get("fundamental").text_hash

    prompt_file.write_text("Edited text — still called v1.")
    edited_hash = registry.get("fundamental").text_hash

    assert original_hash != edited_hash


def test_system_prompt_hash_is_stable():
    registry = PromptRegistry()
    assert registry.system_prompt_hash() == registry.system_prompt_hash()

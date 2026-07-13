"""Prompt registry and versioning tests (docs/milestone-5.md Step 11)."""
from __future__ import annotations

import pytest

from trading_research.research.prompt_registry import PromptNotFoundError, PromptRegistry


def test_registry_loads_shipped_role_prompts():
    registry = PromptRegistry()
    for role in ("fundamental", "technical", "bull", "bear", "manager"):
        prompt = registry.get(role)
        assert prompt.role == role
        assert prompt.version == "v1"
        assert prompt.text_hash
        assert len(prompt.text) > 0


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

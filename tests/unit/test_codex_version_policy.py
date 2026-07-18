"""Unit tests for `research/codex_version_policy.py` (Milestone 12.1 Item 2)."""
from __future__ import annotations

import pytest

from trading_research.research.codex_version_policy import (
    CodexVersionParseError,
    classify_codex_version,
    parse_codex_version,
)


@pytest.mark.parametrize(
    ("raw", "expected_version", "expected_prerelease"),
    [
        ("0.144.5", (0, 144, 5), False),
        ("codex-cli 0.144.5", (0, 144, 5), False),
        ("v0.144.5", (0, 144, 5), False),
        ("0.144.9-beta", (0, 144, 9), True),
        ("0.144.9+build.1", (0, 144, 9), True),
    ],
)
def test_parse_codex_version(raw, expected_version, expected_prerelease):
    version, is_prerelease = parse_codex_version(raw)
    assert version == expected_version
    assert is_prerelease == expected_prerelease


@pytest.mark.parametrize("raw", ["not-a-version", "1", "1.2", "", "abc.def.ghi", "1.2.3.4"])
def test_parse_codex_version_rejects_malformed(raw):
    with pytest.raises(CodexVersionParseError):
        parse_codex_version(raw)


def test_below_supported_range_rejected():
    result = classify_codex_version((0, 144, 4), is_prerelease=False)
    assert result.supported is False
    assert result.adapter_version is None


def test_range_floor_accepted():
    result = classify_codex_version((0, 144, 5), is_prerelease=False)
    assert result.supported is True
    assert result.adapter_version == "codex-jsonl/v1"


def test_within_range_accepted():
    result = classify_codex_version((0, 144, 9), is_prerelease=False)
    assert result.supported is True
    assert result.adapter_version == "codex-jsonl/v1"


def test_range_ceiling_exclusive_rejected():
    result = classify_codex_version((0, 145, 0), is_prerelease=False)
    assert result.supported is False


def test_future_major_version_rejected():
    result = classify_codex_version((1, 0, 0), is_prerelease=False)
    assert result.supported is False


def test_prerelease_rejected_even_within_numeric_range():
    result = classify_codex_version((0, 144, 5), is_prerelease=True)
    assert result.supported is False
    assert "prerelease" in (result.reason or "")

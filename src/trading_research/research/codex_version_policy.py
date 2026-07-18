"""Explicit Codex CLI compatibility policy (Milestone 12.1 Item 2).

The JSONL parser in `codex_jsonl_adapter.py` was validated against exactly
codex-cli 0.144.5's event shapes. An open-ended "any version >= some floor"
check would silently accept a future CLI release whose JSONL contract this
repository has never seen and never tested against. Instead, only the
explicit, closed ranges below are accepted; every future minor/major bump
fails preflight until a maintainer adds a new range here after validating it
(code review is the only way to widen acceptance — this is never
YAML/operator configurable).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^(?:codex-cli\s+)?v?(\d+)\.(\d+)\.(\d+)((?:[-+][0-9A-Za-z.-]+)?)$")

VersionTuple = tuple[int, int, int]


class CodexVersionParseError(ValueError):
    """The supplied string is not a well-formed, non-prerelease semantic version."""


@dataclass(frozen=True)
class VersionRange:
    minimum: VersionTuple
    maximum_exclusive: VersionTuple
    adapter_version: str

    def __post_init__(self) -> None:
        if self.minimum >= self.maximum_exclusive:
            raise ValueError("VersionRange.minimum must be strictly less than maximum_exclusive")
        if not self.adapter_version.strip():
            raise ValueError("VersionRange.adapter_version must be non-empty")


# codex-cli 0.144.5 is the exact version this repository's JSONL adapter
# (codex_jsonl_adapter.py) was validated against on a live smoke test — see
# that module's docstring. 0.144.6 through 0.144.x (exclusive of 0.145.0)
# are accepted on the assumption of patch-level compatibility within the
# same minor version; 0.145.0 and any later minor/major version fails
# closed until it has been independently validated and added here.
SUPPORTED_CODEX_CLI_RANGES: tuple[VersionRange, ...] = (
    VersionRange(minimum=(0, 144, 5), maximum_exclusive=(0, 145, 0), adapter_version="codex-jsonl/v1"),
)


def parse_codex_version(raw: str) -> tuple[VersionTuple, bool]:
    """Returns `(version_tuple, is_prerelease)`. Raises `CodexVersionParseError`
    on anything that is not a well-formed `MAJOR.MINOR.PATCH` string (with an
    optional leading `codex-cli `/`v` prefix this CLI is known to emit)."""
    match = _VERSION_RE.fullmatch(raw.strip())
    if match is None:
        raise CodexVersionParseError(f"{raw!r} is not a well-formed semantic version")
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch)), bool(suffix)


@dataclass(frozen=True)
class CodexVersionClassification:
    supported: bool
    adapter_version: str | None
    reason: str | None


def classify_codex_version(version: VersionTuple, *, is_prerelease: bool) -> CodexVersionClassification:
    """Fails closed: a prerelease/build-tagged version is never accepted (no
    prerelease is currently in `SUPPORTED_CODEX_CLI_RANGES`), and a version
    outside every declared closed-open range is rejected regardless of
    whether it is numerically higher or lower than a supported range."""
    if is_prerelease:
        return CodexVersionClassification(
            supported=False, adapter_version=None,
            reason="prerelease/build-tagged Codex CLI versions are not supported",
        )
    for version_range in SUPPORTED_CODEX_CLI_RANGES:
        if version_range.minimum <= version < version_range.maximum_exclusive:
            return CodexVersionClassification(
                supported=True, adapter_version=version_range.adapter_version, reason=None,
            )
    return CodexVersionClassification(
        supported=False, adapter_version=None,
        reason="Codex CLI version is outside every supported range",
    )


MINIMUM_SUPPORTED_VERSION: VersionTuple = min(r.minimum for r in SUPPORTED_CODEX_CLI_RANGES)


__all__ = [
    "CodexVersionClassification",
    "CodexVersionParseError",
    "MINIMUM_SUPPORTED_VERSION",
    "SUPPORTED_CODEX_CLI_RANGES",
    "VersionRange",
    "VersionTuple",
    "classify_codex_version",
    "parse_codex_version",
]

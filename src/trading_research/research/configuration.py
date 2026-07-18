"""Research-layer configuration (docs/milestone-5.md Step 18).

Mirrors `analysis/screener.py::load_screening_config` / `scorer.py::load_scoring_config`:
YAML + `hash_config` + fail-early on anything malformed. Research defaults to
**disabled**; nothing here reads `ANTHROPIC_API_KEY` or any other
environment variable to silently flip `enabled` or select a provider —
`.env` only ever supplies the credential, never the decision to call Claude.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config
from .errors import UnknownOverlayActionError, UnknownProviderError, UnknownRoleError
from .models import OVERLAY_ACTIONS

DEFAULT_RESEARCH_CONFIG_PATH = REPO_ROOT / "config" / "research.yaml"

KNOWN_PROVIDERS = ("deterministic", "scripted", "anthropic", "claude_code")
KNOWN_ROLES = ("fundamental", "technical", "catalyst", "news", "sentiment", "bull", "bear", "manager")
ANALYST_ROLES = ("fundamental", "technical", "catalyst", "news", "sentiment", "bull", "bear")
MANAGER_ROLE = "manager"


class ResearchConfigError(RuntimeError):
    """The research configuration is missing, malformed, or names an unknown provider/role/action."""


def _strict_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ResearchConfigError(f"{field_name} must be a boolean")
    return value


@dataclass(frozen=True)
class ClaudeCodeYamlConfiguration:
    binary_path: Path
    minimum_version: str
    terminate_grace_seconds: int
    maximum_stdout_bytes: int
    maximum_stderr_bytes: int
    maximum_schema_bytes: int
    maximum_prompt_bytes: int
    maximum_budget_usd_per_call: Decimal
    maximum_turns: int
    working_directory: Path
    require_oauth_authentication: bool
    require_usage_metadata: bool


@dataclass(frozen=True)
class ResearchConfiguration:
    version: int
    enabled: bool
    provider: str
    model: str | None
    max_attempts_per_role: int
    request_timeout_seconds: int
    max_input_characters: int
    max_evidence_items: int
    max_items_per_source_category: int
    max_claims_per_role: int
    max_output_tokens: int
    require_point_in_time_safe: bool
    require_evidence_for_material_claims: bool
    fail_on_stale_required_evidence: bool
    allow_parallel_roles: bool
    roles: tuple[str, ...]
    overlay_policy_version: str
    overlay_allow_score_increase: bool
    overlay_allow_position_size_increase: bool
    overlay_incomplete_action: str
    overlay_critical_risk_action: str
    config_hash: str
    raw: dict
    claude_code: ClaudeCodeYamlConfiguration | None = None

    def analyst_roles(self) -> tuple[str, ...]:
        return tuple(r for r in self.roles if r != MANAGER_ROLE)

    def require_ready(self) -> None:
        """Raise if this configuration cannot actually be used to run research
        right now — called only at the point research is invoked, never at
        load time (so a disabled/unconfigured file still loads cleanly)."""
        if self.provider in {"anthropic", "claude_code"} and not self.model:
            raise ResearchConfigError(f"research.model must be set explicitly to use provider={self.provider}")
        if self.provider == "claude_code" and self.claude_code is None:
            raise ResearchConfigError("provider=claude_code requires the top-level claude_code configuration")

    def build_claude_code_provider_config(self, *, pricing_entries=()):
        if self.provider != "claude_code" or self.claude_code is None:
            raise ResearchConfigError("research.provider is not claude_code")
        from .claude_code_provider import ClaudeCodeProviderConfig

        cfg = self.claude_code
        return ClaudeCodeProviderConfig(
            binary_path=cfg.binary_path,
            minimum_version=cfg.minimum_version,
            request_timeout_seconds=self.request_timeout_seconds,
            terminate_grace_seconds=cfg.terminate_grace_seconds,
            maximum_stdout_bytes=cfg.maximum_stdout_bytes,
            maximum_stderr_bytes=cfg.maximum_stderr_bytes,
            maximum_schema_bytes=cfg.maximum_schema_bytes,
            maximum_prompt_bytes=cfg.maximum_prompt_bytes,
            maximum_budget_usd_per_call=cfg.maximum_budget_usd_per_call,
            maximum_turns=cfg.maximum_turns,
            working_directory=cfg.working_directory,
            pricing_entries=tuple(pricing_entries),
            require_oauth_authentication=cfg.require_oauth_authentication,
            require_usage_metadata=cfg.require_usage_metadata,
            model_alias=self.model or "",
        )


def load_research_config(path: str | Path | None = None) -> ResearchConfiguration:
    config_path = Path(path) if path else DEFAULT_RESEARCH_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ResearchConfigError(f"cannot read research config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ResearchConfigError(f"invalid YAML in research config at {config_path}: {exc}") from exc

    research_section = raw.get("research")
    if not isinstance(research_section, dict):
        raise ResearchConfigError("research config missing top-level 'research' section")
    roles = raw.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ResearchConfigError("research config missing non-empty top-level 'roles' list")
    overlay = raw.get("overlay")
    if not isinstance(overlay, dict):
        raise ResearchConfigError("research config missing top-level 'overlay' section")

    allowed_top_level = {"version", "research", "claude_code", "roles", "overlay"}
    unknown_top_level = set(raw) - allowed_top_level
    if unknown_top_level:
        raise ResearchConfigError(f"research config has unknown top-level keys: {sorted(unknown_top_level)}")

    provider = research_section.get("provider")
    if provider not in KNOWN_PROVIDERS:
        raise UnknownProviderError(f"research.provider {provider!r} is not one of {KNOWN_PROVIDERS} — fails closed")

    for role in roles:
        if role not in KNOWN_ROLES:
            raise UnknownRoleError(f"roles entry {role!r} is not one of {KNOWN_ROLES} — fails closed")

    incomplete_action = overlay.get("incomplete_action")
    critical_risk_action = overlay.get("critical_risk_action")
    for action, field_name in ((incomplete_action, "incomplete_action"), (critical_risk_action, "critical_risk_action")):
        if action not in OVERLAY_ACTIONS:
            raise UnknownOverlayActionError(
                f"overlay.{field_name} {action!r} is not one of {OVERLAY_ACTIONS} — fails closed"
            )

    required_research_keys = {
        "enabled", "provider", "model", "max_attempts_per_role", "request_timeout_seconds",
        "max_input_characters", "max_evidence_items", "max_items_per_source_category",
        "max_claims_per_role", "max_output_tokens", "require_point_in_time_safe",
        "require_evidence_for_material_claims", "fail_on_stale_required_evidence", "allow_parallel_roles",
    }
    missing = required_research_keys - research_section.keys()
    if missing:
        raise ResearchConfigError(f"research config missing keys under 'research': {sorted(missing)}")
    unknown_research = set(research_section) - required_research_keys
    if unknown_research:
        raise ResearchConfigError(f"research config has unknown keys under 'research': {sorted(unknown_research)}")

    claude_code_config = None
    claude_code_section = raw.get("claude_code")
    if claude_code_section is not None:
        if not isinstance(claude_code_section, dict):
            raise ResearchConfigError("claude_code must be a mapping")
        required_claude_code_keys = {
            "binary_path", "minimum_version", "terminate_grace_seconds", "maximum_stdout_bytes",
            "maximum_stderr_bytes", "maximum_schema_bytes", "maximum_prompt_bytes",
            "maximum_budget_usd_per_call", "maximum_turns", "working_directory",
            "require_oauth_authentication", "require_usage_metadata",
        }
        missing_claude_code = required_claude_code_keys - set(claude_code_section)
        unknown_claude_code = set(claude_code_section) - required_claude_code_keys
        if missing_claude_code:
            raise ResearchConfigError(f"claude_code config missing keys: {sorted(missing_claude_code)}")
        if unknown_claude_code:
            raise ResearchConfigError(f"claude_code config has unknown keys: {sorted(unknown_claude_code)}")
        try:
            maximum_budget = Decimal(str(claude_code_section["maximum_budget_usd_per_call"]))
        except (InvalidOperation, ValueError) as exc:
            raise ResearchConfigError("claude_code.maximum_budget_usd_per_call must be a decimal") from exc
        claude_code_config = ClaudeCodeYamlConfiguration(
            binary_path=Path(str(claude_code_section["binary_path"])),
            minimum_version=str(claude_code_section["minimum_version"]),
            terminate_grace_seconds=int(claude_code_section["terminate_grace_seconds"]),
            maximum_stdout_bytes=int(claude_code_section["maximum_stdout_bytes"]),
            maximum_stderr_bytes=int(claude_code_section["maximum_stderr_bytes"]),
            maximum_schema_bytes=int(claude_code_section["maximum_schema_bytes"]),
            maximum_prompt_bytes=int(claude_code_section["maximum_prompt_bytes"]),
            maximum_budget_usd_per_call=maximum_budget,
            maximum_turns=int(claude_code_section["maximum_turns"]),
            working_directory=Path(str(claude_code_section["working_directory"])),
            require_oauth_authentication=_strict_bool(
                claude_code_section["require_oauth_authentication"], "claude_code.require_oauth_authentication"
            ),
            require_usage_metadata=_strict_bool(
                claude_code_section["require_usage_metadata"], "claude_code.require_usage_metadata"
            ),
        )

    return ResearchConfiguration(
        version=raw.get("version", 1),
        enabled=_strict_bool(research_section["enabled"], "research.enabled"),
        provider=provider,
        model=research_section.get("model"),
        max_attempts_per_role=int(research_section["max_attempts_per_role"]),
        request_timeout_seconds=int(research_section["request_timeout_seconds"]),
        max_input_characters=int(research_section["max_input_characters"]),
        max_evidence_items=int(research_section["max_evidence_items"]),
        max_items_per_source_category=int(research_section["max_items_per_source_category"]),
        max_claims_per_role=int(research_section["max_claims_per_role"]),
        max_output_tokens=int(research_section["max_output_tokens"]),
        require_point_in_time_safe=_strict_bool(research_section["require_point_in_time_safe"], "research.require_point_in_time_safe"),
        require_evidence_for_material_claims=_strict_bool(research_section["require_evidence_for_material_claims"], "research.require_evidence_for_material_claims"),
        fail_on_stale_required_evidence=_strict_bool(research_section["fail_on_stale_required_evidence"], "research.fail_on_stale_required_evidence"),
        allow_parallel_roles=_strict_bool(research_section["allow_parallel_roles"], "research.allow_parallel_roles"),
        roles=tuple(roles),
        overlay_policy_version=str(overlay["policy_version"]),
        overlay_allow_score_increase=_strict_bool(overlay.get("allow_score_increase", False), "overlay.allow_score_increase"),
        overlay_allow_position_size_increase=_strict_bool(overlay.get("allow_position_size_increase", False), "overlay.allow_position_size_increase"),
        overlay_incomplete_action=str(incomplete_action),
        overlay_critical_risk_action=str(critical_risk_action),
        config_hash=hash_config(raw),
        raw=raw,
        claude_code=claude_code_config,
    )

"""Research-layer configuration (docs/milestone-5.md Step 18).

Mirrors `analysis/screener.py::load_screening_config` / `scorer.py::load_scoring_config`:
YAML + `hash_config` + fail-early on anything malformed. Research defaults to
**disabled**; nothing here reads `ANTHROPIC_API_KEY` or any other
environment variable to silently flip `enabled` or select a provider —
`.env` only ever supplies the credential, never the decision to call Claude.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config
from .errors import UnknownOverlayActionError, UnknownProviderError, UnknownRoleError
from .models import OVERLAY_ACTIONS

DEFAULT_RESEARCH_CONFIG_PATH = REPO_ROOT / "config" / "research.yaml"

KNOWN_PROVIDERS = ("deterministic", "scripted", "anthropic")
KNOWN_ROLES = ("fundamental", "technical", "catalyst", "news", "sentiment", "bull", "bear", "manager")
ANALYST_ROLES = ("fundamental", "technical", "catalyst", "news", "sentiment", "bull", "bear")
MANAGER_ROLE = "manager"


class ResearchConfigError(RuntimeError):
    """The research configuration is missing, malformed, or names an unknown provider/role/action."""


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

    def analyst_roles(self) -> tuple[str, ...]:
        return tuple(r for r in self.roles if r != MANAGER_ROLE)

    def require_ready(self) -> None:
        """Raise if this configuration cannot actually be used to run research
        right now — called only at the point research is invoked, never at
        load time (so a disabled/unconfigured file still loads cleanly)."""
        if self.provider == "anthropic" and not self.model:
            raise ResearchConfigError("research.model must be set explicitly to use provider=anthropic")


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

    return ResearchConfiguration(
        version=raw.get("version", 1),
        enabled=bool(research_section["enabled"]),
        provider=provider,
        model=research_section.get("model"),
        max_attempts_per_role=int(research_section["max_attempts_per_role"]),
        request_timeout_seconds=int(research_section["request_timeout_seconds"]),
        max_input_characters=int(research_section["max_input_characters"]),
        max_evidence_items=int(research_section["max_evidence_items"]),
        max_items_per_source_category=int(research_section["max_items_per_source_category"]),
        max_claims_per_role=int(research_section["max_claims_per_role"]),
        max_output_tokens=int(research_section["max_output_tokens"]),
        require_point_in_time_safe=bool(research_section["require_point_in_time_safe"]),
        require_evidence_for_material_claims=bool(research_section["require_evidence_for_material_claims"]),
        fail_on_stale_required_evidence=bool(research_section["fail_on_stale_required_evidence"]),
        allow_parallel_roles=bool(research_section["allow_parallel_roles"]),
        roles=tuple(roles),
        overlay_policy_version=str(overlay["policy_version"]),
        overlay_allow_score_increase=bool(overlay.get("allow_score_increase", False)),
        overlay_allow_position_size_increase=bool(overlay.get("allow_position_size_increase", False)),
        overlay_incomplete_action=incomplete_action,
        overlay_critical_risk_action=critical_risk_action,
        config_hash=hash_config(raw),
        raw=raw,
    )

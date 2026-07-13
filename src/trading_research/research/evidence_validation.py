"""Prompt-injection defense and pre-flight evidence validation
(docs/milestone-5.md Steps 4 and 6).

Two independent layers, deliberately not relying on model self-assessment
(Step 6: "Do not use model self-assessment as the only prompt-injection
defense"):

1. `validate_snapshot_preconditions` — deterministic Python checks that can
   fail closed *before* any provider call (missing required evidence, stale
   required evidence, point-in-time unsafe evidence).
2. `render_evidence_for_prompt` — every piece of untrusted evidence text is
   quoted between explicit delimiters, annotated with an injection-risk
   score (reusing `collection/prompt_injection_filter.py`'s pattern list),
   and evidence content is never concatenated into the system prompt.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ..collection.prompt_injection_filter import analyze
from ..models.source_models import InjectionRisk
from .models import EvidenceItem, EvidenceSnapshot

EVIDENCE_OPEN_DELIMITER = "<<<UNTRUSTED_EVIDENCE_DATA"
EVIDENCE_CLOSE_DELIMITER = "UNTRUSTED_EVIDENCE_DATA>>>"

# Applied before delimiting: obvious control-token / fake-closing-delimiter
# neutralization (Step 6: "removal or neutralization of obvious control
# tokens where practical"). Deliberately narrow — this augments, it does not
# replace, the delimiter + annotation defense above.
_NEUTRALIZED_TOKENS = (
    EVIDENCE_OPEN_DELIMITER,
    EVIDENCE_CLOSE_DELIMITER,
    "<|",
    "|>",
    "[SYSTEM]",
    "[/SYSTEM]",
)


@dataclass(frozen=True)
class RenderedEvidenceItem:
    evidence_id: str
    source_id: str
    category: str
    injection_risk: InjectionRisk
    matched_patterns: tuple[str, ...]
    safe_for_summarization: bool
    rendered_text: str


def _neutralize(text: str) -> str:
    cleaned = text
    for token in _NEUTRALIZED_TOKENS:
        cleaned = cleaned.replace(token, f"[neutralized:{token.strip('<>|[]/')}]")
    # Strip C0/C1 control characters (excluding \n, \t) and Unicode
    # format/control categories (Cf/Cc, e.g. RIGHT-TO-LEFT OVERRIDE,
    # zero-width joiners) — Unicode control-character smuggling (Step 6 /
    # testing category C).
    cleaned = "".join(
        ch for ch in cleaned
        if ch in ("\n", "\t") or (ch >= " " and unicodedata.category(ch) not in ("Cf", "Cc"))
    )
    return cleaned


def render_evidence_item(item: EvidenceItem) -> RenderedEvidenceItem:
    """Never executed as an instruction: the returned `rendered_text` is
    always wrapped in explicit untrusted-data delimiters and prefixed with
    its evidence_id, so a role prompt can quote it directly while stating
    (see `prompts/research/*/v1.txt`) that anything between the delimiters
    is data, never a command."""
    annotation = analyze(f"{item.title}\n{item.summary}")
    body = _neutralize(f"{item.title}\n{item.summary}")
    rendered = (
        f"{EVIDENCE_OPEN_DELIMITER} evidence_id={item.evidence_id} "
        f"source_id={item.source_id} category={item.category}\n"
        f"{body}\n"
        f"{EVIDENCE_CLOSE_DELIMITER}"
    )
    return RenderedEvidenceItem(
        evidence_id=item.evidence_id,
        source_id=item.source_id,
        category=item.category,
        injection_risk=annotation.prompt_injection_risk,
        matched_patterns=tuple(annotation.matched_patterns),
        safe_for_summarization=annotation.safe_for_summarization,
        rendered_text=rendered,
    )


def render_evidence_snapshot(snapshot: EvidenceSnapshot) -> tuple[RenderedEvidenceItem, ...]:
    return tuple(render_evidence_item(item) for item in snapshot.evidence_items)


def validate_snapshot_preconditions(
    snapshot: EvidenceSnapshot,
    *,
    require_point_in_time_safe: bool,
    fail_on_stale_required_evidence: bool,
    required_categories: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Deterministic pre-flight checks the orchestrator runs *before* any
    provider call. Returns a tuple of reasons; a non-empty tuple means the
    run must short-circuit to ANALYSIS_INCOMPLETE without ever contacting a
    provider (docs/milestone-5.md: "no provider invocation where failure is
    detectable beforehand")."""
    reasons: list[str] = []

    if snapshot.missing_data_reasons:
        reasons.extend(f"missing required evidence: {r}" for r in snapshot.missing_data_reasons)

    if require_point_in_time_safe and not snapshot.point_in_time_safe:
        reasons.append("snapshot is not point-in-time safe")

    if fail_on_stale_required_evidence:
        stale_ids = [item.evidence_id for item in snapshot.evidence_items if item.stale]
        if stale_ids and required_categories:
            stale_required = [
                item.evidence_id
                for item in snapshot.evidence_items
                if item.stale and item.category in required_categories
            ]
            if stale_required:
                reasons.append(f"required evidence is stale: {sorted(stale_required)}")

    present_categories = {item.category for item in snapshot.evidence_items}
    for category in required_categories:
        if category not in present_categories:
            reasons.append(f"required evidence category missing: {category}")

    return tuple(reasons)

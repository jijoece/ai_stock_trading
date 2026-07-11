"""Flags likely prompt-injection content in untrusted external text (Reddit posts/comments).

This never deletes or rewrites the source text — it only annotates it. The
annotation travels with the record so downstream batch prompts can wrap
high-risk text as explicitly-quoted untrusted data (see prompts/workstream-user.md)
instead of ever concatenating it into an instruction context.
"""
from __future__ import annotations

import re

from ..models.source_models import InjectionRisk, PromptInjectionAnnotation

# (pattern, risk contribution) — risk is escalated by the highest match, not summed.
_PATTERNS: list[tuple[re.Pattern, InjectionRisk]] = [
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I), InjectionRisk.HIGH),
    (re.compile(r"system\s*prompt", re.I), InjectionRisk.HIGH),
    (re.compile(r"reveal\s+(the\s+)?(secrets?|api\s*keys?|tokens?|credentials?)", re.I), InjectionRisk.HIGH),
    (re.compile(r"use\s+this\s+token", re.I), InjectionRisk.HIGH),
    (re.compile(r"transfer\s+(money|funds|\$)", re.I), InjectionRisk.HIGH),
    (re.compile(r"change\s+your\s+rules", re.I), InjectionRisk.HIGH),
    (re.compile(r"call\s+this\s+tool", re.I), InjectionRisk.MEDIUM),
    (re.compile(r"execute\s+this\s+(command|code|script)", re.I), InjectionRisk.MEDIUM),
    (re.compile(r"place\s+an?\s+order", re.I), InjectionRisk.MEDIUM),
    (re.compile(r"buy\s+this\s+stock", re.I), InjectionRisk.MEDIUM),
    (re.compile(r"\bas\s+an?\s+ai\b.{0,40}\byou\s+(must|should|will)\b", re.I), InjectionRisk.MEDIUM),
    (re.compile(r"base64|rot13|\\x[0-9a-f]{2}", re.I), InjectionRisk.LOW),  # possible obfuscated payload
]

_RISK_ORDER = {InjectionRisk.NONE: 0, InjectionRisk.LOW: 1, InjectionRisk.MEDIUM: 2, InjectionRisk.HIGH: 3}


def analyze(text: str) -> PromptInjectionAnnotation:
    if not text:
        return PromptInjectionAnnotation()

    matched: list[str] = []
    worst = InjectionRisk.NONE
    for pattern, risk in _PATTERNS:
        if pattern.search(text):
            matched.append(pattern.pattern)
            if _RISK_ORDER[risk] > _RISK_ORDER[worst]:
                worst = risk

    return PromptInjectionAnnotation(
        prompt_injection_risk=worst,
        matched_patterns=matched,
        safe_for_summarization=worst != InjectionRisk.HIGH,
    )

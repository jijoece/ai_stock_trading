"""Data models for normalized Reddit records and catalog sources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class InjectionRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class PromptInjectionAnnotation:
    prompt_injection_risk: InjectionRisk = InjectionRisk.NONE
    matched_patterns: list[str] = field(default_factory=list)
    safe_for_summarization: bool = True

    def to_dict(self) -> dict:
        return {
            "prompt_injection_risk": self.prompt_injection_risk.value,
            "matched_patterns": self.matched_patterns,
            "safe_for_summarization": self.safe_for_summarization,
        }


@dataclass
class RedditRecord:
    source_type: str = "reddit"
    subreddit: str = ""
    post_id: str = ""
    comment_id: str | None = None
    title: str = ""
    body: str = ""
    author_hash: str = ""
    created_at: str = ""
    retrieved_at: str = ""
    score: int = 0
    comment_count: int = 0
    permalink: str = ""
    query: str = ""
    sort: str = ""
    time_filter: str = ""
    is_comment: bool = False
    parent_id: str | None = None
    injection: PromptInjectionAnnotation = field(default_factory=PromptInjectionAnnotation)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["injection"] = self.injection.to_dict()
        return d

    def dedup_key(self) -> str:
        return f"reddit:{self.comment_id}" if self.is_comment and self.comment_id else f"reddit:{self.post_id}"


@dataclass
class CatalogSource:
    source_id: str
    title: str
    url: str
    date: str
    source_type: str
    reliability: str
    verified_or_anecdotal: str
    key_finding: str

    def to_dict(self) -> dict:
        return asdict(self)

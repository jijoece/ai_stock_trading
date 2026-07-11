"""Data models mirroring schemas/batch_workstream_result.schema.json.

These are thin dataclasses used after JSON Schema validation has already
passed (see processing/result_validator.py) — they exist for typed access
during consolidation, not as the validation layer itself.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ConfirmedFact:
    claim: str
    evidence: str
    source_ids: list[str]
    source_type: str
    confidence: str
    limitations: str = ""


@dataclass
class RedditObservation:
    observation: str
    source_ids: list[str]
    support_level: str
    subreddits: list[str]
    approximate_dates: list[str]
    potential_bias: str = ""
    confidence: str = "low"


@dataclass
class Contradiction:
    topic: str
    position_a: str
    position_b: str
    source_ids_a: list[str]
    source_ids_b: list[str]
    assessment: str = ""


@dataclass
class Recommendation:
    recommendation: str
    rationale: str
    confidence: str
    dependencies: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass
class RiskItem:
    risk: str
    severity: str
    likelihood: str
    mitigation: str = ""


@dataclass
class WorkstreamResult:
    workstream_id: str
    title: str
    summary: str
    confirmed_facts: list[ConfirmedFact] = field(default_factory=list)
    reddit_observations: list[RedditObservation] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    risks: list[RiskItem] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkstreamResult":
        return cls(
            workstream_id=d["workstream_id"],
            title=d["title"],
            summary=d["summary"],
            confirmed_facts=[ConfirmedFact(**f) for f in d.get("confirmed_facts", [])],
            reddit_observations=[RedditObservation(**o) for o in d.get("reddit_observations", [])],
            contradictions=[Contradiction(**c) for c in d.get("contradictions", [])],
            recommendations=[Recommendation(**r) for r in d.get("recommendations", [])],
            risks=[RiskItem(**r) for r in d.get("risks", [])],
            open_questions=list(d.get("open_questions", [])),
            sources=list(d.get("sources", [])),
        )

    def to_dict(self) -> dict:
        return {
            "workstream_id": self.workstream_id,
            "title": self.title,
            "summary": self.summary,
            "confirmed_facts": [asdict(f) for f in self.confirmed_facts],
            "reddit_observations": [asdict(o) for o in self.reddit_observations],
            "contradictions": [asdict(c) for c in self.contradictions],
            "recommendations": [asdict(r) for r in self.recommendations],
            "risks": [asdict(r) for r in self.risks],
            "open_questions": self.open_questions,
            "sources": self.sources,
        }

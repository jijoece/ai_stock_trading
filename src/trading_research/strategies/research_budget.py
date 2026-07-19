"""Token-budgeted research handoff decisions (Milestone 23, B6).

Deterministic, zero-LLM pre-filter that runs *before* a shortlisted
candidate is ever handed to `research/orchestration.py`. It does not
duplicate that module's own fresh-run reuse logic
(`compute_research_run_id` / `reused_existing_run`) — it only decides
whether a candidate is even eligible to be sent today. `REUSE_RESEARCH` vs
`RUN_FULL_RESEARCH` is recorded by the caller after invoking the existing
orchestrator, based on its `reused_existing_run` flag.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .config import ShortlistConfig
from .selector import ShortlistEntry


class ResearchDecision(str, Enum):
    SEND_TO_RESEARCH = "SEND_TO_RESEARCH"
    REUSE_RESEARCH = "REUSE_RESEARCH"
    RUN_FULL_RESEARCH = "RUN_FULL_RESEARCH"
    RUN_REDUCED_CONTEXT = "RUN_REDUCED_CONTEXT"  # defined for forward-compatibility; no trigger path yet
    SKIP_LOW_SIGNAL = "SKIP_LOW_SIGNAL"
    SKIP_DAILY_CANDIDATE_CAP = "SKIP_DAILY_CANDIDATE_CAP"
    SKIP_DAILY_TOKEN_CAP = "SKIP_DAILY_TOKEN_CAP"


@dataclass(frozen=True)
class DailyResearchBudgetState:
    """Caller-supplied daily counters; not persisted by this module."""

    symbols_sent_today: int
    fresh_cycles_today: int
    daily_token_budget: int | None = None
    tokens_spent_today: int = 0

    def token_budget_remaining(self) -> bool:
        if self.daily_token_budget is None:
            return True
        return self.tokens_spent_today < self.daily_token_budget


@dataclass(frozen=True)
class ResearchBudgetDecision:
    symbol: str
    decision: ResearchDecision
    reason: str


def decide_research_action(
    entry: ShortlistEntry,
    state: DailyResearchBudgetState,
    config: ShortlistConfig,
) -> ResearchBudgetDecision:
    if entry.best_signal_strength < config.minimum_signal_strength_for_research:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_LOW_SIGNAL,
            f"signal_strength {entry.best_signal_strength} below "
            f"minimum_signal_strength_for_research {config.minimum_signal_strength_for_research}",
        )
    if state.symbols_sent_today >= config.maximum_symbols_to_research_per_day:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_CANDIDATE_CAP,
            f"symbols_sent_today {state.symbols_sent_today} >= "
            f"maximum_symbols_to_research_per_day {config.maximum_symbols_to_research_per_day}",
        )
    if state.fresh_cycles_today >= config.maximum_fresh_research_cycles_per_day:
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_CANDIDATE_CAP,
            f"fresh_cycles_today {state.fresh_cycles_today} >= "
            f"maximum_fresh_research_cycles_per_day {config.maximum_fresh_research_cycles_per_day}",
        )
    if not state.token_budget_remaining():
        return ResearchBudgetDecision(
            entry.symbol, ResearchDecision.SKIP_DAILY_TOKEN_CAP,
            f"tokens_spent_today {state.tokens_spent_today} >= daily_token_budget {state.daily_token_budget}",
        )
    return ResearchBudgetDecision(entry.symbol, ResearchDecision.SEND_TO_RESEARCH, "within daily research budget")


def record_research_outcome(symbol: str, reused_existing_run: bool) -> ResearchBudgetDecision:
    """Called after invoking `research.orchestration.analyze_with_research_committee`
    for a `SEND_TO_RESEARCH` candidate, to persist the final decision label."""
    decision = ResearchDecision.REUSE_RESEARCH if reused_existing_run else ResearchDecision.RUN_FULL_RESEARCH
    reason = "orchestrator reused an existing research run" if reused_existing_run else "orchestrator ran a fresh research cycle"
    return ResearchBudgetDecision(symbol, decision, reason)

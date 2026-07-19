from dataclasses import replace

from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.research_budget import (
    DailyResearchBudgetState,
    ResearchDecision,
    decide_research_action,
    record_research_outcome,
)
from trading_research.strategies.selector import ShortlistEntry

SHORTLIST_CONFIG = load_strategy_config().shortlist


def _entry(symbol: str = "AAA", strength: float = 0.8) -> ShortlistEntry:
    return ShortlistEntry(symbol=symbol, strategy_signals=(), best_signal_strength=strength,
                           included=True, reason="shortlisted")


def test_fresh_research_reuse_recorded_after_orchestrator_call():
    decision = record_research_outcome("AAA", reused_existing_run=True)
    assert decision.decision == ResearchDecision.REUSE_RESEARCH

    decision = record_research_outcome("AAA", reused_existing_run=False)
    assert decision.decision == ResearchDecision.RUN_FULL_RESEARCH


def test_within_budget_sends_to_research():
    state = DailyResearchBudgetState(symbols_sent_today=0, fresh_cycles_today=0)
    result = decide_research_action(_entry(), state, SHORTLIST_CONFIG)
    assert result.decision == ResearchDecision.SEND_TO_RESEARCH


def test_daily_candidate_cap_skips():
    config = replace(SHORTLIST_CONFIG, maximum_symbols_to_research_per_day=2)
    state = DailyResearchBudgetState(symbols_sent_today=2, fresh_cycles_today=0)
    result = decide_research_action(_entry(), state, config)
    assert result.decision == ResearchDecision.SKIP_DAILY_CANDIDATE_CAP


def test_daily_token_cap_skips():
    state = DailyResearchBudgetState(symbols_sent_today=0, fresh_cycles_today=0,
                                      daily_token_budget=1000, tokens_spent_today=1000)
    result = decide_research_action(_entry(), state, SHORTLIST_CONFIG)
    assert result.decision == ResearchDecision.SKIP_DAILY_TOKEN_CAP


def test_skip_low_signal():
    config = replace(SHORTLIST_CONFIG, minimum_signal_strength_for_research=0.5)
    state = DailyResearchBudgetState(symbols_sent_today=0, fresh_cycles_today=0)
    result = decide_research_action(_entry(strength=0.1), state, config)
    assert result.decision == ResearchDecision.SKIP_LOW_SIGNAL

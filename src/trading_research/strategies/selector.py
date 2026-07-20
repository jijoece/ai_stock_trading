"""Deterministic multi-strategy candidate shortlist (Milestone 23, B6).

Ranks by numeric factors only (`signal_strength`, then `data_quality`, then
freshness) — never by LLM prose. Runs at zero LLM calls: this module does
not import anything from `research/`.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.trading_models import PortfolioState
from .config import ShortlistConfig
from .contracts import StrategySignal, StrategyStatus

_DATA_QUALITY_RANK = {"complete": 0, "incomplete": 1, "stale": 1}


@dataclass(frozen=True)
class ShortlistEntry:
    symbol: str
    strategy_signals: tuple[StrategySignal, ...]
    best_signal_strength: float
    included: bool
    reason: str


@dataclass(frozen=True)
class ShortlistResult:
    entries: tuple[ShortlistEntry, ...]
    excluded: tuple[ShortlistEntry, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.entries)


def _sort_key(signal: StrategySignal) -> tuple[float, int, float]:
    """Milestone 24 Part B2: on a strength/quality tie, the *newer*
    `data_as_of` must rank first. Sorting ascending on the negated epoch
    timestamp achieves that (a larger/newer timestamp negates to a smaller
    — earlier-sorting — value); a missing `data_as_of` sorts last, never
    ahead of a signal with a known, verifiable freshness."""
    quality_rank = _DATA_QUALITY_RANK.get(signal.data_quality, 2)
    freshness_rank = -signal.data_as_of.timestamp() if signal.data_as_of is not None else float("inf")
    return (-signal.signal_strength, quality_rank, freshness_rank)


def _symbol_allocation_decision(
    symbol: str, portfolio: PortfolioState | None, config: ShortlistConfig,
) -> tuple[bool, str | None]:
    """Milestone 24 Part C6: real, deterministic, per-symbol pre-research
    filter — replaces the previous gate, which only checked whether total
    portfolio exposure was below 100% and so let an already-maxed-out
    single symbol through as long as the rest of the account had room. A
    symbol that cannot receive at least `minimum_candidate_allocation_fraction`
    more without breaching `maximum_symbol_allocation_fraction` should not
    consume a research token. Advisory only — final risk validation remains
    authoritative."""
    if portfolio is None:
        return True, None
    if (
        portfolio.account_equity is None
        or portfolio.account_equity <= 0
        or not portfolio.symbol_exposure_complete
    ):
        return False, "symbol_exposure_unknown"
    if symbol in portfolio.existing_positions and symbol not in portfolio.symbol_exposure_fraction:
        return False, "symbol_exposure_unknown"
    # A complete snapshot proves a symbol absent from existing_positions is
    # genuinely unheld, so only that case may use a known zero.
    current = portfolio.symbol_exposure_fraction.get(symbol)
    if current is None:
        if symbol in portfolio.existing_positions:
            return False, "symbol_exposure_unknown"
        current = 0.0
    remaining = config.maximum_symbol_allocation_fraction - current
    return remaining >= config.minimum_candidate_allocation_fraction, (
        None if remaining >= config.minimum_candidate_allocation_fraction else "symbol_allocation_cap_reached"
    )


def select_shortlist(
    signals_by_strategy: dict[str, tuple[StrategySignal, ...]],
    config: ShortlistConfig,
    portfolio: PortfolioState | None = None,
) -> ShortlistResult:
    per_symbol: dict[str, list[StrategySignal]] = {}

    for signals in signals_by_strategy.values():
        eligible = sorted(
            (s for s in signals if s.status == StrategyStatus.ELIGIBLE),
            key=_sort_key,
        )
        capped = eligible[: config.maximum_candidates_per_strategy]
        for signal in capped:
            per_symbol.setdefault(signal.symbol, []).append(signal)

    ranked_symbols = sorted(
        per_symbol.items(),
        key=lambda kv: min(_sort_key(s) for s in kv[1]),
    )

    entries: list[ShortlistEntry] = []
    excluded: list[ShortlistEntry] = []
    for symbol, signals in ranked_symbols:
        best_strength = max(s.signal_strength for s in signals)
        signals_tuple = tuple(signals)
        if len(entries) >= config.maximum_combined_shortlist:
            excluded.append(ShortlistEntry(symbol, signals_tuple, best_strength, False,
                                            "combined_shortlist_cap_reached"))
            continue
        has_room, allocation_reason = _symbol_allocation_decision(symbol, portfolio, config)
        if not has_room:
            excluded.append(ShortlistEntry(symbol, signals_tuple, best_strength, False,
                                            allocation_reason or "symbol_exposure_unknown"))
            continue
        entries.append(ShortlistEntry(symbol, signals_tuple, best_strength, True, "shortlisted"))

    return ShortlistResult(entries=tuple(entries), excluded=tuple(excluded))

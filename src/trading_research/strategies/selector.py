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


def _sort_key(signal: StrategySignal) -> tuple[float, int, str]:
    quality_rank = _DATA_QUALITY_RANK.get(signal.data_quality, 2)
    data_as_of = signal.data_as_of.isoformat() if signal.data_as_of is not None else ""
    return (-signal.signal_strength, quality_rank, data_as_of)


def _symbol_has_allocation_room(symbol: str, portfolio: PortfolioState | None, config: ShortlistConfig) -> bool:
    """Milestone 24 Part C6: real, deterministic, per-symbol pre-research
    filter — replaces the previous gate, which only checked whether total
    portfolio exposure was below 100% and so let an already-maxed-out
    single symbol through as long as the rest of the account had room. A
    symbol that cannot receive at least `minimum_candidate_allocation_fraction`
    more without breaching `maximum_symbol_allocation_fraction` should not
    consume a research token. Advisory only — final risk validation remains
    authoritative."""
    if portfolio is None:
        return True
    current = portfolio.symbol_exposure_fraction.get(symbol, 0.0)
    remaining = config.maximum_symbol_allocation_fraction - current
    return remaining >= config.minimum_candidate_allocation_fraction


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
        if not _symbol_has_allocation_room(symbol, portfolio, config):
            excluded.append(ShortlistEntry(symbol, signals_tuple, best_strength, False,
                                            "symbol_allocation_cap_reached"))
            continue
        entries.append(ShortlistEntry(symbol, signals_tuple, best_strength, True, "shortlisted"))

    return ShortlistResult(entries=tuple(entries), excluded=tuple(excluded))

"""Shared fixtures for tests/unit/test_*_strategy*.py and friends.

Not itself a test module (no `test_` prefix), so pytest does not collect it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from trading_research.analysis.screener import GateResult, ScreeningResult
from trading_research.backtesting.models import HistoricalBar

NOW = datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)


def passing_screening_result(symbol: str = "TEST") -> ScreeningResult:
    gate = GateResult(
        gate="max_share_price", passed=True, hard_failure=False,
        threshold=100.0, observed=50.0, reason="ok", data_timestamp=NOW.isoformat(),
    )
    return ScreeningResult(
        symbol=symbol, passed=True, gate_results=(gate,),
        config_hash="fixturehash", config_version=1, screened_at=NOW.isoformat(),
    )


def stale_screening_result(symbol: str = "TEST") -> ScreeningResult:
    gate = GateResult(
        gate="max_data_staleness_seconds", passed=False, hard_failure=True,
        threshold=600.0, observed=9999.0, reason="data stale", data_timestamp=NOW.isoformat(),
    )
    return ScreeningResult(
        symbol=symbol, passed=False, gate_results=(gate,),
        config_hash="fixturehash", config_version=1, screened_at=NOW.isoformat(),
    )


def failing_screening_result(symbol: str = "TEST", gate_name: str = "min_market_cap") -> ScreeningResult:
    gate = GateResult(
        gate=gate_name, passed=False, hard_failure=True,
        threshold=1.0, observed=0.0, reason="failed", data_timestamp=NOW.isoformat(),
    )
    return ScreeningResult(
        symbol=symbol, passed=False, gate_results=(gate,),
        config_hash="fixturehash", config_version=1, screened_at=NOW.isoformat(),
    )


def build_bars(
    closes: list[float],
    symbol: str = "TEST",
    volumes: list[int] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: date | None = None,
) -> tuple[HistoricalBar, ...]:
    n = len(closes)
    volumes = volumes if volumes is not None else [1_000_000] * n
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    # Milestone 24 Part B1: strategies now reject a bar whose `available_at`
    # is after `context.now` (NOW, above) as future information. Default
    # far enough before NOW that even a long lookback (e.g. a 200-day SMA
    # plus buffer) never runs a bar series past it.
    start = start or date(2024, 1, 1)
    bars = []
    for i in range(n):
        session_date = start + timedelta(days=i)
        opn = closes[i - 1] if i > 0 else closes[i]
        bars.append(HistoricalBar(
            symbol=symbol,
            session_date=session_date,
            open=Decimal(str(round(opn, 4))),
            high=Decimal(str(round(max(highs[i], closes[i], opn), 4))),
            low=Decimal(str(round(min(lows[i], closes[i], opn), 4))),
            close=Decimal(str(round(closes[i], 4))),
            volume=volumes[i],
            available_at=datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=21),
        ))
    return tuple(bars)

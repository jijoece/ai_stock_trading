"""Forward-performance evaluation contracts (docs/milestone-4.md Step 11).

Framework-neutral, independent of LumiBot and of the execution/runtime
boundary — an evaluation only ever reads already-persisted recommendation
and execution data plus historical closing prices; it never talks to a
broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

EVALUATION_HORIZONS = (1, 5, 10, 20, 60)

EVALUATION_STATUSES = (
    "PENDING",
    "COMPLETED",
    "INCOMPLETE_MISSING_DATA",
    "BENCHMARK_MISSING",
    "DELISTED_OR_UNAVAILABLE",
    "NEVER_EXECUTED",
    "PARTIALLY_FILLED",
)

DEFAULT_BENCHMARK_SYMBOL = "SPY"


class EvaluationValidationError(ValueError):
    pass


def _require_tz_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise EvaluationValidationError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class RecommendationEvaluation:
    recommendation_id: str
    horizon_trading_days: int
    status: str
    evaluation_date: date
    benchmark_symbol: str

    recommendation_price: Decimal | None = None
    execution_price: Decimal | None = None
    benchmark_price_at_execution: Decimal | None = None
    ending_symbol_price: Decimal | None = None
    ending_benchmark_price: Decimal | None = None

    gross_return: Decimal | None = None
    net_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    excess_return: Decimal | None = None
    slippage: Decimal | None = None
    fees: Decimal = Decimal("0")
    max_favorable_excursion: Decimal | None = None
    max_adverse_excursion: Decimal | None = None

    missing_data_reasons: tuple[str, ...] = ()
    model_version: str | None = None
    prompt_version: str | None = None
    config_hash: str | None = None
    market_regime: str | None = None
    price_source_as_of: str | None = None

    evaluated_at: datetime | None = None  # required — enforced in __post_init__, not a real default

    def __post_init__(self) -> None:
        if self.horizon_trading_days not in EVALUATION_HORIZONS:
            raise EvaluationValidationError(f"unsupported horizon {self.horizon_trading_days} — fail closed")
        if self.status not in EVALUATION_STATUSES:
            raise EvaluationValidationError(f"unknown evaluation status {self.status!r} — fail closed")
        if self.evaluated_at is None:
            raise EvaluationValidationError("evaluated_at is required")
        _require_tz_aware(self.evaluated_at, "evaluated_at")
        if self.status != "COMPLETED" and self.status != "PARTIALLY_FILLED" and (
            self.gross_return is not None or self.net_return is not None or self.excess_return is not None
        ):
            raise EvaluationValidationError(
                f"status {self.status!r} must not carry computed returns"
            )
        if self.status in ("COMPLETED", "PARTIALLY_FILLED") and self.gross_return is None:
            raise EvaluationValidationError(f"status {self.status!r} requires gross_return to be computed")
        if self.status in ("INCOMPLETE_MISSING_DATA", "BENCHMARK_MISSING", "DELISTED_OR_UNAVAILABLE") and not self.missing_data_reasons:
            raise EvaluationValidationError(f"status {self.status!r} must record at least one missing_data_reason")
        if self.fees < 0:
            raise EvaluationValidationError("fees must not be negative")

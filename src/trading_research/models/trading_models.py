"""Typed input snapshots shared by the screener, scorer, and risk engine.

Every field that can legitimately be unknown is `Optional` and defaults to
`None` — never to a favorable numeric default (architecture §2, §14: "a
stock cannot pass merely because data is unavailable"). Timestamps are
timezone-aware UTC. Money fields use `Decimal` (screener/scorer thresholds
compare against these); `risk/position_sizing.py` keeps its own tested float
arithmetic unchanged — see docs/milestone2-analysis-layer.md for why.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class DataFreshness:
    """Audit trail for one data source feeding a decision."""

    source: str
    as_of: datetime  # tz-aware UTC; the timestamp of the data itself
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError(f"DataFreshness.as_of for {self.source!r} must be timezone-aware")

    def age_seconds(self, now: datetime) -> float:
        return (now - self.as_of).total_seconds()

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        return self.age_seconds(now) > max_age_seconds


@dataclass(frozen=True)
class SecuritySnapshot:
    symbol: str
    name: str
    exchange: str
    sector: str = ""
    is_otc: bool | None = None
    is_active: bool | None = None
    listing_date: datetime | None = None  # None = unknown, explicit
    freshness: DataFreshness | None = None


@dataclass(frozen=True)
class MarketDataSnapshot:
    symbol: str
    price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    avg_daily_dollar_volume: Decimal | None = None
    market_cap: Decimal | None = None
    is_halted: bool | None = None
    recent_halt: bool | None = None
    recent_reverse_split: bool | None = None
    realized_volatility: float | None = None  # e.g. 20d stdev of daily returns
    freshness: DataFreshness | None = None

    @property
    def spread_bps(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        if mid == 0:
            return None
        return float((self.ask - self.bid) / mid * Decimal(10_000))


@dataclass(frozen=True)
class FundamentalSnapshot:
    symbol: str
    as_of: datetime | None = None
    revenue_growth_yoy: float | None = None
    earnings_trend: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    free_cash_flow: Decimal | None = None
    cash: Decimal | None = None
    debt: Decimal | None = None
    quarterly_cash_burn: Decimal | None = None
    shares_outstanding: Decimal | None = None
    shares_outstanding_prior_year: Decimal | None = None
    operating_history_years: float | None = None
    going_concern_warning: bool | None = None
    bankruptcy_or_distress: bool | None = None
    shell_company_flag: bool | None = None
    recent_reverse_split: bool | None = None
    valuation_percentile_vs_sector: float | None = None
    freshness: DataFreshness | None = None

    @property
    def cash_runway_quarters(self) -> float | None:
        if self.cash is None or self.quarterly_cash_burn is None or self.quarterly_cash_burn <= 0:
            return None
        return float(self.cash / self.quarterly_cash_burn)

    @property
    def share_growth_yoy(self) -> float | None:
        if self.shares_outstanding is None or not self.shares_outstanding_prior_year:
            return None
        return float(
            (self.shares_outstanding - self.shares_outstanding_prior_year)
            / self.shares_outstanding_prior_year
        )


@dataclass(frozen=True)
class TechnicalFactorInput:
    symbol: str
    relative_strength: float | None = None   # e.g. vs sector/benchmark, arbitrary scale
    momentum_score: float | None = None      # e.g. scripts/score.py momentum pillar, -2..+2
    trend_score: float | None = None         # e.g. scripts/score.py trend pillar, -2..+2
    price_volume_trend: float | None = None
    volatility_flag: bool | None = None      # abnormal volatility, explicit unknown allowed
    freshness: DataFreshness | None = None


@dataclass(frozen=True)
class CatalystRiskFlags:
    symbol: str
    upcoming_catalysts: tuple[str, ...] = ()
    earnings_date: datetime | None = None
    earnings_date_known: bool = False
    days_to_earnings: float | None = None
    analyst_estimate_change: float | None = None
    sec_filing_risk_flags: tuple[str, ...] = ()
    verified_news_flags: tuple[str, ...] = ()
    macro_score: float | None = None  # scripts/macro_pillar.py output, -2..+2
    freshness: DataFreshness | None = None


@dataclass(frozen=True)
class PortfolioPositionSnapshot:
    """Point-in-time inputs needed to prove one symbol's market value."""

    quantity: int | None
    market_price: Decimal | None
    price_as_of: datetime | None


@dataclass(frozen=True)
class PortfolioAccountSnapshot:
    """Explicit, completeness-aware account/positions read (docs/milestones/26.md
    B1). An empty `positions` dict is ambiguous on its own — it can mean a
    verified-empty account or a query that never ran — so every field a
    caller needs to prove which one occurred is carried explicitly rather
    than inferred from dict emptiness."""

    account_equity: Decimal | None
    settled_cash: Decimal | None
    account_as_of: datetime | None
    positions: dict[str, "PortfolioPositionSnapshot"]
    positions_as_of: datetime | None
    account_identity: str | None
    account_verified: bool
    account_query_complete: bool
    positions_query_complete: bool
    source: str  # "fixture" | "paper_ledger" | "broker_read_only" | "unknown"


@dataclass(frozen=True)
class PortfolioState:
    """Typed snapshot of account/portfolio state, mapped by callers into the
    flat fields `risk/position_sizing.RiskInputs` expects. Kept separate from
    RiskInputs so the well-tested, flat risk-math function doesn't need to
    change shape — see docs/milestone2-analysis-layer.md.
    """

    account_equity: Decimal | None
    settled_cash: Decimal | None
    existing_positions: dict[str, int] = field(default_factory=dict)  # symbol -> shares held
    sector_exposure_fraction: dict[str, float] = field(default_factory=dict)  # sector -> fraction of equity
    symbol_exposure_fraction: dict[str, float] = field(default_factory=dict)  # symbol -> fraction of equity
    symbol_exposure_complete: bool = False
    portfolio_exposure_fraction: float | None = None
    daily_loss_fraction: float = 0.0
    drawdown_fraction: float = 0.0
    as_of: datetime | None = None
    portfolio_source: str | None = None  # docs/milestones/26.md B4: audit-only provenance label

    def shares_held(self, symbol: str) -> int | None:
        return self.existing_positions.get(symbol, 0)

    @classmethod
    def from_position_snapshots(
        cls,
        *,
        account_equity: Decimal | None,
        settled_cash: Decimal | None,
        positions: dict[str, PortfolioPositionSnapshot],
        as_of: datetime,
        maximum_price_age_seconds: int,
    ) -> "PortfolioState":
        """Build a fail-closed per-symbol exposure snapshot.

        Any missing/nonpositive equity, quantity, price, or stale/future
        price makes the whole exposure map incomplete. Partial values remain
        visible for audit but cannot be treated as favorable zero exposure.
        """
        existing_positions = {
            symbol: snapshot.quantity
            for symbol, snapshot in positions.items()
            if type(snapshot.quantity) is int and snapshot.quantity > 0
        }
        exposures: dict[str, float] = {}
        complete = (
            account_equity is not None and account_equity > 0
            and as_of.tzinfo is not None
            and type(maximum_price_age_seconds) is int and maximum_price_age_seconds >= 0
        )
        if complete:
            for symbol, snapshot in positions.items():
                quantity = snapshot.quantity
                price = snapshot.market_price
                price_as_of = snapshot.price_as_of
                if (
                    type(quantity) is not int or quantity <= 0
                    or price is None or price <= 0
                    or price_as_of is None or price_as_of.tzinfo is None
                    or price_as_of > as_of
                    or (as_of - price_as_of).total_seconds() > maximum_price_age_seconds
                ):
                    complete = False
                    break
                exposures[symbol] = float(Decimal(quantity) * price / account_equity)
        return cls(
            account_equity=account_equity, settled_cash=settled_cash,
            existing_positions=existing_positions, symbol_exposure_fraction=exposures,
            symbol_exposure_complete=complete,
            portfolio_exposure_fraction=sum(exposures.values()) if complete else None,
            as_of=as_of,
        )


def build_portfolio_state(
    snapshot: PortfolioAccountSnapshot,
    *,
    as_of: datetime,
    maximum_account_age_seconds: int,
    maximum_positions_age_seconds: int,
    maximum_position_price_age_seconds: int,
) -> PortfolioState:
    """Explicit completeness-aware builder (docs/milestones/26.md B1/B2).

    Unlike `PortfolioState.from_position_snapshots`, an empty `positions`
    dict on `snapshot` only proves zero holdings when the snapshot itself
    proves the account and positions queries were verified, complete, and
    fresh as of `as_of` — otherwise exposure stays unknown, never a
    favorable zero (architecture §2/§14).
    """
    existing_positions = {
        symbol: p.quantity
        for symbol, p in snapshot.positions.items()
        if type(p.quantity) is int and p.quantity > 0
    }

    def _fresh(ts: datetime | None, maximum_age_seconds: int) -> bool:
        return (
            ts is not None and ts.tzinfo is not None and ts <= as_of
            and (as_of - ts).total_seconds() <= maximum_age_seconds
        )

    complete = (
        snapshot.account_verified
        and snapshot.account_query_complete
        and snapshot.positions_query_complete
        and bool(snapshot.account_identity)
        and snapshot.account_equity is not None and snapshot.account_equity > 0
        and _fresh(snapshot.account_as_of, maximum_account_age_seconds)
        and _fresh(snapshot.positions_as_of, maximum_positions_age_seconds)
    )

    exposures: dict[str, float] = {}
    if complete:
        account_equity = snapshot.account_equity
        assert account_equity is not None  # narrowed by `complete` above
        for symbol, p in snapshot.positions.items():
            quantity = p.quantity
            if type(quantity) is not int or quantity <= 0:
                continue
            price = p.market_price
            if (
                price is None or price <= 0
                or not _fresh(p.price_as_of, maximum_position_price_age_seconds)
            ):
                complete = False
                exposures = {}
                break
            exposures[symbol] = float(Decimal(quantity) * price / account_equity)

    return PortfolioState(
        account_equity=snapshot.account_equity, settled_cash=snapshot.settled_cash,
        existing_positions=existing_positions, symbol_exposure_fraction=exposures,
        symbol_exposure_complete=complete,
        portfolio_exposure_fraction=sum(exposures.values()) if complete else None,
        as_of=as_of, portfolio_source=snapshot.source,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

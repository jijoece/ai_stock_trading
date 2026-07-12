"""Deterministic stock screener (architecture §14, story 1C.1).

The screener applies hard eligibility gates only. It does not rank
candidates and does not produce a trade recommendation — that is the
scorer's (scorer.py) and recommendation builder's job. Every gate in this
slice is a hard failure: unknown critical input fails the gate closed (never
a favorable default), and every gate is evaluated and recorded regardless of
whether an earlier gate already failed, so gate evaluation order never
changes the final result and every outcome is preserved for auditability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from ..hashing import hash_config
from ..models.trading_models import (
    CatalystRiskFlags,
    FundamentalSnapshot,
    MarketDataSnapshot,
    SecuritySnapshot,
    TechnicalFactorInput,
)

DEFAULT_SCREENING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "screening.yaml"


class ScreeningConfigError(RuntimeError):
    """The screening configuration is missing, malformed, or out of range."""


@dataclass(frozen=True)
class ScreeningConfig:
    version: int
    max_share_price: float
    min_market_cap: float
    min_avg_daily_dollar_volume: float
    min_operating_history_years: float
    exclude_otc: bool
    exclude_inactive: bool
    exclude_bankruptcy_or_distress: bool
    exclude_going_concern_warning: bool
    exclude_shell_company: bool
    exclude_recent_reverse_split: bool
    max_dilution_share_growth_yoy: float
    min_cash_runway_quarters: float
    earnings_blackout_days: float
    max_realized_volatility: float
    exclude_recent_halt: bool
    max_bid_ask_spread_bps: float
    max_data_staleness_seconds: float
    config_hash: str
    raw: dict

    def __post_init__(self) -> None:
        for name in (
            "max_share_price", "min_market_cap", "min_avg_daily_dollar_volume",
            "min_operating_history_years", "max_dilution_share_growth_yoy",
            "min_cash_runway_quarters", "earnings_blackout_days",
            "max_realized_volatility", "max_bid_ask_spread_bps", "max_data_staleness_seconds",
        ):
            if getattr(self, name) < 0:
                raise ScreeningConfigError(f"screening config {name!r} must be >= 0")


def load_screening_config(path: str | Path | None = None) -> ScreeningConfig:
    """Load and validate config/screening.yaml. Fails early on any problem."""
    config_path = Path(path) if path else DEFAULT_SCREENING_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ScreeningConfigError(f"cannot read screening config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ScreeningConfigError(f"invalid YAML in screening config at {config_path}: {exc}") from exc

    required = {
        "version", "max_share_price", "min_market_cap", "min_avg_daily_dollar_volume",
        "min_operating_history_years", "exclude_otc", "exclude_inactive",
        "exclude_bankruptcy_or_distress", "exclude_going_concern_warning",
        "exclude_shell_company", "exclude_recent_reverse_split",
        "max_dilution_share_growth_yoy", "min_cash_runway_quarters",
        "earnings_blackout_days", "max_realized_volatility", "exclude_recent_halt",
        "max_bid_ask_spread_bps", "max_data_staleness_seconds",
    }
    missing = required - raw.keys()
    if missing:
        raise ScreeningConfigError(f"screening config missing keys: {sorted(missing)}")

    return ScreeningConfig(
        version=raw["version"],
        max_share_price=float(raw["max_share_price"]),
        min_market_cap=float(raw["min_market_cap"]),
        min_avg_daily_dollar_volume=float(raw["min_avg_daily_dollar_volume"]),
        min_operating_history_years=float(raw["min_operating_history_years"]),
        exclude_otc=bool(raw["exclude_otc"]),
        exclude_inactive=bool(raw["exclude_inactive"]),
        exclude_bankruptcy_or_distress=bool(raw["exclude_bankruptcy_or_distress"]),
        exclude_going_concern_warning=bool(raw["exclude_going_concern_warning"]),
        exclude_shell_company=bool(raw["exclude_shell_company"]),
        exclude_recent_reverse_split=bool(raw["exclude_recent_reverse_split"]),
        max_dilution_share_growth_yoy=float(raw["max_dilution_share_growth_yoy"]),
        min_cash_runway_quarters=float(raw["min_cash_runway_quarters"]),
        earnings_blackout_days=float(raw["earnings_blackout_days"]),
        max_realized_volatility=float(raw["max_realized_volatility"]),
        exclude_recent_halt=bool(raw["exclude_recent_halt"]),
        max_bid_ask_spread_bps=float(raw["max_bid_ask_spread_bps"]),
        max_data_staleness_seconds=float(raw["max_data_staleness_seconds"]),
        config_hash=hash_config(raw),
        raw=raw,
    )


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    hard_failure: bool
    threshold: object
    observed: object
    reason: str
    data_timestamp: str | None


@dataclass(frozen=True)
class ScreeningCandidate:
    security: SecuritySnapshot
    market: MarketDataSnapshot
    fundamentals: FundamentalSnapshot
    technical: TechnicalFactorInput | None = None
    catalyst: CatalystRiskFlags | None = None


@dataclass(frozen=True)
class ScreeningResult:
    symbol: str
    passed: bool
    gate_results: tuple[GateResult, ...]
    config_hash: str
    config_version: int
    screened_at: str


def _ts(freshness) -> str | None:
    return freshness.as_of.isoformat() if freshness is not None else None


def _gate(gate: str, passed: bool, threshold: object, observed: object, reason: str, data_timestamp: str | None) -> GateResult:
    return GateResult(
        gate=gate, passed=passed, hard_failure=not passed,
        threshold=threshold, observed=observed, reason=reason, data_timestamp=data_timestamp,
    )


def screen_candidate(
    candidate: ScreeningCandidate,
    config: ScreeningConfig,
    now: datetime,
) -> ScreeningResult:
    """Evaluate every configured gate independently and return every outcome.

    Unknown critical input for a gate is itself a hard failure for that gate
    (fail closed) — a candidate never passes a gate merely because the
    underlying data is unavailable.
    """
    sec, mkt, fnd = candidate.security, candidate.market, candidate.fundamentals
    tech, cat = candidate.technical, candidate.catalyst
    gates: list[GateResult] = []

    price = mkt.price
    gates.append(_gate(
        "max_share_price", price is not None and price <= config.max_share_price,
        config.max_share_price, price,
        "price unknown" if price is None else f"price {price} vs cap {config.max_share_price}",
        _ts(mkt.freshness),
    ))

    mc = mkt.market_cap
    gates.append(_gate(
        "min_market_cap", mc is not None and mc >= config.min_market_cap,
        config.min_market_cap, mc,
        "market cap unknown" if mc is None else f"market cap {mc} vs floor {config.min_market_cap}",
        _ts(mkt.freshness),
    ))

    adv = mkt.avg_daily_dollar_volume
    gates.append(_gate(
        "min_avg_daily_dollar_volume", adv is not None and adv >= config.min_avg_daily_dollar_volume,
        config.min_avg_daily_dollar_volume, adv,
        "avg daily dollar volume unknown" if adv is None
        else f"avg daily dollar volume {adv} vs floor {config.min_avg_daily_dollar_volume}",
        _ts(mkt.freshness),
    ))

    history = fnd.operating_history_years
    gates.append(_gate(
        "min_operating_history_years", history is not None and history >= config.min_operating_history_years,
        config.min_operating_history_years, history,
        "operating history unknown" if history is None
        else f"operating history {history}y vs floor {config.min_operating_history_years}y",
        _ts(fnd.freshness),
    ))

    if config.exclude_otc:
        gates.append(_gate(
            "exclude_otc", sec.is_otc is False,
            False, sec.is_otc,
            "OTC status unknown" if sec.is_otc is None else f"is_otc={sec.is_otc}",
            _ts(sec.freshness),
        ))

    if config.exclude_inactive:
        gates.append(_gate(
            "exclude_inactive", sec.is_active is True,
            True, sec.is_active,
            "active status unknown" if sec.is_active is None else f"is_active={sec.is_active}",
            _ts(sec.freshness),
        ))

    if config.exclude_bankruptcy_or_distress:
        flag = fnd.bankruptcy_or_distress
        gates.append(_gate(
            "exclude_bankruptcy_or_distress", flag is False,
            False, flag,
            "bankruptcy/distress status unknown" if flag is None else f"bankruptcy_or_distress={flag}",
            _ts(fnd.freshness),
        ))

    if config.exclude_going_concern_warning:
        flag = fnd.going_concern_warning
        gates.append(_gate(
            "exclude_going_concern_warning", flag is False,
            False, flag,
            "going-concern status unknown" if flag is None else f"going_concern_warning={flag}",
            _ts(fnd.freshness),
        ))

    if config.exclude_shell_company:
        flag = fnd.shell_company_flag
        gates.append(_gate(
            "exclude_shell_company", flag is False,
            False, flag,
            "shell-company status unknown" if flag is None else f"shell_company_flag={flag}",
            _ts(fnd.freshness),
        ))

    if config.exclude_recent_reverse_split:
        flag = fnd.recent_reverse_split if fnd.recent_reverse_split is not None else mkt.recent_reverse_split
        gates.append(_gate(
            "exclude_recent_reverse_split", flag is False,
            False, flag,
            "reverse-split status unknown" if flag is None else f"recent_reverse_split={flag}",
            _ts(fnd.freshness),
        ))

    growth = fnd.share_growth_yoy
    gates.append(_gate(
        "max_dilution_share_growth_yoy", growth is not None and growth <= config.max_dilution_share_growth_yoy,
        config.max_dilution_share_growth_yoy, growth,
        "share growth unknown" if growth is None
        else f"share growth {growth:.2%} vs cap {config.max_dilution_share_growth_yoy:.2%}",
        _ts(fnd.freshness),
    ))

    runway = fnd.cash_runway_quarters
    gates.append(_gate(
        "min_cash_runway_quarters", runway is not None and runway >= config.min_cash_runway_quarters,
        config.min_cash_runway_quarters, runway,
        "cash runway unknown" if runway is None
        else f"cash runway {runway:.1f}q vs floor {config.min_cash_runway_quarters}q",
        _ts(fnd.freshness),
    ))

    days_to_earnings = cat.days_to_earnings if cat is not None else None
    earnings_known = cat.earnings_date_known if cat is not None else False
    earnings_ok = earnings_known and days_to_earnings is not None and days_to_earnings >= config.earnings_blackout_days
    gates.append(_gate(
        "earnings_blackout_days", earnings_ok,
        config.earnings_blackout_days, days_to_earnings,
        "earnings date unknown" if not earnings_known
        else f"{days_to_earnings}d to earnings vs blackout {config.earnings_blackout_days}d",
        _ts(cat.freshness) if cat is not None else None,
    ))

    realized_vol = mkt.realized_volatility
    gates.append(_gate(
        "max_realized_volatility", realized_vol is not None and realized_vol <= config.max_realized_volatility,
        config.max_realized_volatility, realized_vol,
        "realized volatility unknown" if realized_vol is None
        else f"realized volatility {realized_vol:.2%} vs cap {config.max_realized_volatility:.2%}",
        _ts(mkt.freshness),
    ))

    if config.exclude_recent_halt:
        flag = mkt.recent_halt
        gates.append(_gate(
            "exclude_recent_halt", flag is False,
            False, flag,
            "halt status unknown" if flag is None else f"recent_halt={flag}",
            _ts(mkt.freshness),
        ))

    spread = mkt.spread_bps
    gates.append(_gate(
        "max_bid_ask_spread_bps", spread is not None and spread <= config.max_bid_ask_spread_bps,
        config.max_bid_ask_spread_bps, spread,
        "spread unknown (missing/invalid bid-ask)" if spread is None
        else f"spread {spread:.0f}bps vs cap {config.max_bid_ask_spread_bps:.0f}bps",
        _ts(mkt.freshness),
    ))

    freshnesses = [f for f in (sec.freshness, mkt.freshness, fnd.freshness) if f is not None]
    stale = any(f.is_stale(now, config.max_data_staleness_seconds) for f in freshnesses)
    all_present = sec.freshness is not None and mkt.freshness is not None and fnd.freshness is not None
    gates.append(_gate(
        "max_data_staleness_seconds", all_present and not stale,
        config.max_data_staleness_seconds,
        max((f.age_seconds(now) for f in freshnesses), default=None),
        "one or more data-freshness timestamps missing" if not all_present
        else ("data stale" if stale else "data fresh"),
        _ts(mkt.freshness),
    ))

    return ScreeningResult(
        symbol=sec.symbol,
        passed=all(g.passed for g in gates),
        gate_results=tuple(gates),
        config_hash=config.config_hash,
        config_version=config.version,
        screened_at=now.isoformat(),
    )

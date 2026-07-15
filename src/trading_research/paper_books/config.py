"""Loads `config/paper_books.yaml` (docs/milestone-8.md Step 3).

Same fail-closed, `hash_config`-stamped pattern as `shadow/config.py` and
`evidence_providers/config.py`. `paper_books.enabled` and the enhanced book's
own `enabled` both default `false`. `execution.allow_live_broker` is
structurally impossible to be `true` — `ExecutionSection.__post_init__`
raises if it is ever `true`, exactly like
`shadow/config.py::ShadowOperationsSection.__post_init__`'s
`allow_enhanced_submission` guard. No environment variable is read anywhere
in this module to decide a capability — `.env` only ever supplies a
credential (there are none here at all), never a capability decision.
Unrecognized top-level/section keys fail closed (typo protection), matching
this milestone's own explicit "unknown keys fail closed" requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config

DEFAULT_PAPER_BOOKS_CONFIG_PATH = REPO_ROOT / "config" / "paper_books.yaml"

BOOK_ID_BASELINE = "BASELINE"
BOOK_ID_ENHANCED = "ENHANCED"
KNOWN_BOOK_IDS = (BOOK_ID_BASELINE, BOOK_ID_ENHANCED)

KNOWN_EXECUTION_PROVIDERS = ("local_simulated", "external_paper_broker")
KNOWN_PRICE_SOURCES = ("evidence_snapshot", "persisted_market_bar")
KNOWN_MISSING_PRICE_POLICIES = ("MARK_UNVALUED",)


class PaperBooksConfigError(RuntimeError):
    pass


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaperBooksConfigError(f"{field_name} is not a valid decimal: {value!r}") from exc


def _require_no_unknown_keys(section: dict, known: set[str], section_name: str) -> None:
    unknown = section.keys() - known
    if unknown:
        raise PaperBooksConfigError(f"{section_name} has unknown keys: {sorted(unknown)} — fails closed")


@dataclass(frozen=True)
class PaperBookDefinition:
    enabled: bool
    book_id: str
    starting_cash_usd: Decimal

    def __post_init__(self) -> None:
        if self.book_id not in KNOWN_BOOK_IDS:
            raise PaperBooksConfigError(f"book_id {self.book_id!r} is not one of {KNOWN_BOOK_IDS} — fails closed")
        if self.starting_cash_usd <= 0:
            raise PaperBooksConfigError(f"starting_cash_usd for {self.book_id} must be > 0")


@dataclass(frozen=True)
class ExecutionSection:
    provider: str
    allow_external_paper_broker: bool
    allow_live_broker: bool

    def __post_init__(self) -> None:
        if self.allow_live_broker:
            raise PaperBooksConfigError(
                "paper_books.execution.allow_live_broker=true is not permitted — fails closed"
            )
        if self.provider not in KNOWN_EXECUTION_PROVIDERS:
            raise PaperBooksConfigError(
                f"execution.provider {self.provider!r} is not one of {KNOWN_EXECUTION_PROVIDERS} — fails closed"
            )
        if self.provider == "external_paper_broker" and not self.allow_external_paper_broker:
            raise PaperBooksConfigError(
                "execution.provider=external_paper_broker requires allow_external_paper_broker=true"
            )


@dataclass(frozen=True)
class RiskSection:
    max_position_weight: Decimal
    max_order_notional_usd: Decimal
    max_daily_new_notional_usd: Decimal
    minimum_cash_buffer_weight: Decimal
    max_open_positions: int
    max_symbol_concentration_weight: Decimal
    reject_stale_market_price_seconds: int

    def __post_init__(self) -> None:
        for field_name in ("max_position_weight", "minimum_cash_buffer_weight", "max_symbol_concentration_weight"):
            value = getattr(self, field_name)
            if not (Decimal("0") <= value <= Decimal("1")):
                raise PaperBooksConfigError(f"risk.{field_name} must be in [0,1] — got {value}")
        for field_name in ("max_order_notional_usd", "max_daily_new_notional_usd"):
            if getattr(self, field_name) <= 0:
                raise PaperBooksConfigError(f"risk.{field_name} must be > 0")
        if self.max_open_positions <= 0:
            raise PaperBooksConfigError("risk.max_open_positions must be > 0")
        if self.reject_stale_market_price_seconds <= 0:
            raise PaperBooksConfigError("risk.reject_stale_market_price_seconds must be > 0")


@dataclass(frozen=True)
class ValuationSection:
    price_source: str
    maximum_price_age_seconds: int
    missing_price_policy: str

    def __post_init__(self) -> None:
        if self.price_source not in KNOWN_PRICE_SOURCES:
            raise PaperBooksConfigError(
                f"valuation.price_source {self.price_source!r} is not one of {KNOWN_PRICE_SOURCES} — fails closed"
            )
        if self.missing_price_policy not in KNOWN_MISSING_PRICE_POLICIES:
            raise PaperBooksConfigError(
                f"valuation.missing_price_policy {self.missing_price_policy!r} is not one of "
                f"{KNOWN_MISSING_PRICE_POLICIES} — fails closed"
            )
        if self.maximum_price_age_seconds <= 0:
            raise PaperBooksConfigError("valuation.maximum_price_age_seconds must be > 0")


@dataclass(frozen=True)
class ScheduledIntegrationSection:
    """Milestone 8.1 gate for the real-scheduled-cycle-to-paper-books
    integration path — distinct from `paper_books.enabled` (which also
    gates the fixture-mode `paper-book-run-cycle` CLI command).
    `enabled` defaults `False` and this whole section is OPTIONAL in the raw
    YAML (absent entirely = disabled), so every pre-existing config fixture
    that predates this section keeps loading unchanged."""

    enabled: bool


@dataclass(frozen=True)
class PaperBooksConfiguration:
    version: int
    enabled: bool
    baseline: PaperBookDefinition
    enhanced: PaperBookDefinition
    execution: ExecutionSection
    risk: RiskSection
    valuation: ValuationSection
    scheduled_integration: ScheduledIntegrationSection
    config_hash: str
    raw: dict

    def __post_init__(self) -> None:
        if self.baseline.book_id == self.enhanced.book_id:
            raise PaperBooksConfigError("baseline and enhanced books must not share the same book_id")

    def book(self, book_id: str) -> PaperBookDefinition:
        if book_id == self.baseline.book_id:
            return self.baseline
        if book_id == self.enhanced.book_id:
            return self.enhanced
        raise PaperBooksConfigError(f"unknown book_id {book_id!r}")

    def is_book_enabled(self, book_id: str) -> bool:
        return self.enabled and self.book(book_id).enabled


def load_paper_books_config(path: str | Path | None = None) -> PaperBooksConfiguration:
    config_path = Path(path) if path else DEFAULT_PAPER_BOOKS_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise PaperBooksConfigError(f"cannot read paper-books config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PaperBooksConfigError(f"invalid YAML in paper-books config at {config_path}: {exc}") from exc

    _require_no_unknown_keys(raw, {"version", "paper_books"}, "paper-books config")

    pb = raw.get("paper_books")
    if not isinstance(pb, dict):
        raise PaperBooksConfigError("paper-books config missing top-level 'paper_books' section")

    _require_no_unknown_keys(
        pb, {"enabled", "books", "execution", "risk", "valuation", "scheduled_integration"}, "paper_books"
    )

    books = pb.get("books")
    if not isinstance(books, dict):
        raise PaperBooksConfigError("paper_books config missing 'books' section")
    _require_no_unknown_keys(books, {"baseline", "enhanced"}, "paper_books.books")

    execution = pb.get("execution")
    risk = pb.get("risk")
    valuation = pb.get("valuation")
    if not isinstance(execution, dict):
        raise PaperBooksConfigError("paper_books config missing 'execution' section")
    if not isinstance(risk, dict):
        raise PaperBooksConfigError("paper_books config missing 'risk' section")
    if not isinstance(valuation, dict):
        raise PaperBooksConfigError("paper_books config missing 'valuation' section")

    _require_no_unknown_keys(execution, {"provider", "allow_external_paper_broker", "allow_live_broker"}, "execution")
    _require_no_unknown_keys(
        risk,
        {
            "max_position_weight", "max_order_notional_usd", "max_daily_new_notional_usd",
            "minimum_cash_buffer_weight", "max_open_positions", "max_symbol_concentration_weight",
            "reject_stale_market_price_seconds",
        },
        "risk",
    )
    _require_no_unknown_keys(valuation, {"price_source", "maximum_price_age_seconds", "missing_price_policy"}, "valuation")

    try:
        baseline_raw = books.get("baseline")
        enhanced_raw = books.get("enhanced")
        if not isinstance(baseline_raw, dict) or not isinstance(enhanced_raw, dict):
            raise PaperBooksConfigError("paper_books.books requires both 'baseline' and 'enhanced' sections")
        for name, section in (("baseline", baseline_raw), ("enhanced", enhanced_raw)):
            _require_no_unknown_keys(section, {"enabled", "book_id", "starting_cash_usd"}, f"books.{name}")

        baseline = PaperBookDefinition(
            enabled=bool(baseline_raw["enabled"]), book_id=str(baseline_raw["book_id"]),
            starting_cash_usd=_decimal(baseline_raw["starting_cash_usd"], "books.baseline.starting_cash_usd"),
        )
        enhanced = PaperBookDefinition(
            enabled=bool(enhanced_raw["enabled"]), book_id=str(enhanced_raw["book_id"]),
            starting_cash_usd=_decimal(enhanced_raw["starting_cash_usd"], "books.enhanced.starting_cash_usd"),
        )
        execution_section = ExecutionSection(
            provider=str(execution["provider"]),
            allow_external_paper_broker=bool(execution["allow_external_paper_broker"]),
            allow_live_broker=bool(execution["allow_live_broker"]),
        )
        risk_section = RiskSection(
            max_position_weight=_decimal(risk["max_position_weight"], "risk.max_position_weight"),
            max_order_notional_usd=_decimal(risk["max_order_notional_usd"], "risk.max_order_notional_usd"),
            max_daily_new_notional_usd=_decimal(risk["max_daily_new_notional_usd"], "risk.max_daily_new_notional_usd"),
            minimum_cash_buffer_weight=_decimal(risk["minimum_cash_buffer_weight"], "risk.minimum_cash_buffer_weight"),
            max_open_positions=int(risk["max_open_positions"]),
            max_symbol_concentration_weight=_decimal(
                risk["max_symbol_concentration_weight"], "risk.max_symbol_concentration_weight"
            ),
            reject_stale_market_price_seconds=int(risk["reject_stale_market_price_seconds"]),
        )
        valuation_section = ValuationSection(
            price_source=str(valuation["price_source"]),
            maximum_price_age_seconds=int(valuation["maximum_price_age_seconds"]),
            missing_price_policy=str(valuation["missing_price_policy"]),
        )

        scheduled_integration_raw = pb.get("scheduled_integration", {})
        if not isinstance(scheduled_integration_raw, dict):
            raise PaperBooksConfigError("paper_books.scheduled_integration must be a mapping")
        _require_no_unknown_keys(scheduled_integration_raw, {"enabled"}, "scheduled_integration")
        scheduled_integration_section = ScheduledIntegrationSection(
            enabled=bool(scheduled_integration_raw.get("enabled", False)),
        )
    except PaperBooksConfigError:
        raise
    except Exception as exc:
        raise PaperBooksConfigError(f"invalid paper-books config value: {exc}") from exc

    return PaperBooksConfiguration(
        version=raw.get("version", 1), enabled=bool(pb["enabled"]), baseline=baseline, enhanced=enhanced,
        execution=execution_section, risk=risk_section, valuation=valuation_section,
        scheduled_integration=scheduled_integration_section,
        config_hash=hash_config(raw), raw=raw,
    )

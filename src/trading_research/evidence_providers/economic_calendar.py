"""Framework-neutral, point-in-time-safe economic calendar boundary."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..hashing import hash_config

IMPORTANCE_LOW = "LOW"
IMPORTANCE_MEDIUM = "MEDIUM"
IMPORTANCE_HIGH = "HIGH"
KNOWN_IMPORTANCE = (IMPORTANCE_LOW, IMPORTANCE_MEDIUM, IMPORTANCE_HIGH)
IMPORTANCE_RANK = {name: index for index, name in enumerate(KNOWN_IMPORTANCE)}

EVENT_STATUS_SCHEDULED = "SCHEDULED"
EVENT_STATUS_RELEASED = "RELEASED"
EVENT_STATUS_CANCELLED = "CANCELLED"
KNOWN_EVENT_STATUSES = (EVENT_STATUS_SCHEDULED, EVENT_STATUS_RELEASED, EVENT_STATUS_CANCELLED)

BLACKOUT_ALLOWED = "ALLOWED"
BLACKOUT_BLOCKED_EVENT = "BLOCKED_HIGH_IMPACT_EVENT"
BLACKOUT_BLOCKED_UNKNOWN = "BLOCKED_EVENT_STATE_UNKNOWN"
BLACKOUT_POLICY_VERSION = "economic-event-blackout-v1"
EXTERNAL_PROVIDER_STATUS = "ENVIRONMENTALLY_PENDING"


class EconomicCalendarError(ValueError):
    pass


@dataclass(frozen=True)
class EconomicEvent:
    event_id: str
    title: str
    category: str
    market: str
    scheduled_at: datetime
    originally_published_at: datetime
    last_updated_at: datetime
    importance: str
    status: str
    actual_value: str | None
    forecast_value: str | None
    previous_value: str | None
    source_provider: str
    source_locator: str
    retrieved_at: datetime
    available_at: datetime
    point_in_time_safe: bool
    content_hash: str

    def __post_init__(self) -> None:
        for name in (
            "scheduled_at", "originally_published_at", "last_updated_at",
            "retrieved_at", "available_at",
        ):
            if getattr(self, name).tzinfo is None:
                raise EconomicCalendarError(f"{name} must be timezone-aware")
        if not self.event_id or not self.title or not self.category or not self.market:
            raise EconomicCalendarError("event identity, title, category, and market are required")
        if self.importance not in KNOWN_IMPORTANCE:
            raise EconomicCalendarError(f"importance must be one of {KNOWN_IMPORTANCE}")
        if self.status not in KNOWN_EVENT_STATUSES:
            raise EconomicCalendarError(f"status must be one of {KNOWN_EVENT_STATUSES}")
        if not self.content_hash:
            raise EconomicCalendarError("content_hash is required")


class EconomicCalendarProvider(Protocol):
    def fetch_events(
        self, *, start: datetime, end: datetime, as_of: datetime,
    ) -> tuple[EconomicEvent, ...]: ...


@dataclass(frozen=True)
class EconomicEventBlackoutConfiguration:
    enabled: bool
    before_minutes: int
    after_minutes: int
    minimum_importance: str
    markets: tuple[str, ...]
    blocked_categories: tuple[str, ...]
    unknown_event_state_action: str = "BLOCK_NEW_ENTRIES"
    maximum_data_age_minutes: int = 1440

    def __post_init__(self) -> None:
        if self.before_minutes < 0 or self.after_minutes < 0:
            raise EconomicCalendarError("blackout minute windows must not be negative")
        if self.minimum_importance not in KNOWN_IMPORTANCE:
            raise EconomicCalendarError(f"minimum_importance must be one of {KNOWN_IMPORTANCE}")
        if self.unknown_event_state_action != "BLOCK_NEW_ENTRIES":
            raise EconomicCalendarError("unknown event state must fail closed with BLOCK_NEW_ENTRIES")
        if not self.markets or not self.blocked_categories:
            raise EconomicCalendarError("markets and blocked_categories must be non-empty")
        if self.maximum_data_age_minutes <= 0:
            raise EconomicCalendarError("maximum_data_age_minutes must be positive")


@dataclass(frozen=True)
class EconomicEventBlackoutDecision:
    allowed: bool
    matched_event_ids: tuple[str, ...]
    blackout_start: datetime | None
    blackout_end: datetime | None
    reason_codes: tuple[str, ...]
    policy_version: str
    configuration_hash: str


def evaluate_economic_event_blackout(
    *, as_of: datetime, events: tuple[EconomicEvent, ...] | None,
    configuration: EconomicEventBlackoutConfiguration,
) -> EconomicEventBlackoutDecision:
    """Pure deterministic BUY-entry blackout evaluation.

    The caller applies this decision only to new exposure; risk-reducing
    SELLs bypass the blackout by design.
    """
    if as_of.tzinfo is None:
        raise EconomicCalendarError("as_of must be timezone-aware")
    config_hash = hash_config({
        "enabled": configuration.enabled,
        "before_minutes": configuration.before_minutes,
        "after_minutes": configuration.after_minutes,
        "minimum_importance": configuration.minimum_importance,
        "markets": list(configuration.markets),
        "blocked_categories": list(configuration.blocked_categories),
        "unknown_event_state_action": configuration.unknown_event_state_action,
        "maximum_data_age_minutes": configuration.maximum_data_age_minutes,
    })

    def decision(
        allowed: bool, reasons: tuple[str, ...], matched: tuple[str, ...] = (),
        start: datetime | None = None, end: datetime | None = None,
    ) -> EconomicEventBlackoutDecision:
        return EconomicEventBlackoutDecision(
            allowed=allowed, matched_event_ids=matched, blackout_start=start, blackout_end=end,
            reason_codes=reasons, policy_version=BLACKOUT_POLICY_VERSION,
            configuration_hash=config_hash,
        )

    if not configuration.enabled:
        return decision(True, ("BLACKOUT_DISABLED",))
    if events is None:
        return decision(False, (BLACKOUT_BLOCKED_UNKNOWN, "EVENT_DATA_UNAVAILABLE"))

    unsafe: list[str] = []
    matches: list[tuple[EconomicEvent, datetime, datetime]] = []
    minimum_rank = IMPORTANCE_RANK[configuration.minimum_importance]
    for event in sorted(events, key=lambda item: (item.scheduled_at, item.event_id)):
        if (
            not event.point_in_time_safe
            or event.available_at > as_of
            or event.originally_published_at > as_of
            or event.last_updated_at > as_of
            or event.retrieved_at > as_of
        ):
            unsafe.append(event.event_id)
            continue
        if as_of - event.retrieved_at > timedelta(minutes=configuration.maximum_data_age_minutes):
            unsafe.append(event.event_id)
            continue
        if event.status == EVENT_STATUS_CANCELLED:
            continue
        if event.market not in configuration.markets:
            continue
        if event.category not in configuration.blocked_categories:
            continue
        if IMPORTANCE_RANK[event.importance] < minimum_rank:
            continue
        start = event.scheduled_at - timedelta(minutes=configuration.before_minutes)
        end = event.scheduled_at + timedelta(minutes=configuration.after_minutes)
        if start <= as_of <= end:
            matches.append((event, start, end))

    if unsafe:
        return decision(
            False, (BLACKOUT_BLOCKED_UNKNOWN, "EVENT_DATA_STALE_OR_POINT_IN_TIME_UNSAFE"),
            tuple(sorted(unsafe)),
        )
    if not matches:
        return decision(True, ("NO_MATCHING_HIGH_IMPACT_EVENT",))
    return decision(
        False, (BLACKOUT_BLOCKED_EVENT,), tuple(item[0].event_id for item in matches),
        min(item[1] for item in matches), max(item[2] for item in matches),
    )


def blackout_decision_id(book_id: str, order_evaluation_id: str, as_of: datetime) -> str:
    payload = f"{book_id}:{order_evaluation_id}:{as_of.isoformat()}:{BLACKOUT_POLICY_VERSION}"
    return "econ-blackout-" + hashlib.sha256(payload.encode()).hexdigest()[:40]

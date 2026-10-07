"""Pure deterministic catalog, time, and booking-policy decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from receptionist.domain.config import (
    BookingPolicySpec,
    BookingRequirementSpec,
    BusinessConfigSpec,
    ServiceAreaSpec,
    ServiceSpec,
    WeeklyHours,
    normalize_catalog_text,
)
from receptionist.domain.enums import BookingStatus, Weekday
from receptionist.integrations.google_calendar import CalendarInterval

_OUTSIDE_BUSINESS_POLICY: Final = "outside_business_policy"
_MAX_DETAIL_LENGTH: Final = 500


class CatalogSelectorAmbiguous(ValueError):
    """Raised when one normalized catalog label maps to multiple codes."""


@dataclass(frozen=True)
class ServiceCatalogView:
    """Minimal application-owned service view needed by pure validation."""

    booking_requirements: Sequence[BookingRequirementSpec]


@dataclass(frozen=True)
class BusinessConfigView:
    """Minimal application-owned business view needed by pure policy evaluation."""

    timezone: str
    weekly_hours: WeeklyHours
    booking_policy: BookingPolicySpec


@dataclass(frozen=True)
class RequestedInterval:
    """A normalized, ordered, timezone-aware UTC interval."""

    start_at_utc: datetime
    end_at_utc: datetime

    def __post_init__(self) -> None:
        start_at_utc = _normalize_aware_instant(self.start_at_utc)
        end_at_utc = _normalize_aware_instant(self.end_at_utc)
        if end_at_utc <= start_at_utc:
            raise ValueError("requested interval must have a positive duration")
        object.__setattr__(self, "start_at_utc", start_at_utc)
        object.__setattr__(self, "end_at_utc", end_at_utc)


@dataclass(frozen=True)
class PolicyDecision:
    """The deterministic result of applying business policy to an interval."""

    valid: bool
    error_code: str | None
    effective_interval: RequestedInterval

    @property
    def allowed(self) -> bool:
        """Alias that makes the decision readable at application call sites."""
        return self.valid


@dataclass(frozen=True)
class AvailabilityDecision:
    """The bounded result of composing policy with provider interval truth."""

    policy_valid: bool
    provider_available: bool
    error_code: str | None
    effective_interval: RequestedInterval


def _normalize_aware_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")

    timezone_info = value.tzinfo
    if isinstance(timezone_info, ZoneInfo):
        wall_time = value.replace(tzinfo=None)
        valid_offsets = {
            candidate.utcoffset()
            for fold in (0, 1)
            if (candidate := value.replace(fold=fold))
            .astimezone(UTC)
            .astimezone(timezone_info)
            .replace(tzinfo=None)
            == wall_time
        }
        if not valid_offsets:
            raise ValueError("datetime represents a nonexistent local time")
        if len(valid_offsets) > 1:
            raise ValueError("datetime represents an ambiguous local time")

    return value.astimezone(UTC)


def normalize_requested_interval(
    start_at: datetime, end_at: datetime | None, duration_minutes: int
) -> RequestedInterval:
    """Normalize aware inputs to UTC and derive an omitted end from duration."""
    if isinstance(duration_minutes, bool) or duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    start_at_utc = _normalize_aware_instant(start_at)
    end_at_utc = (
        _normalize_aware_instant(end_at)
        if end_at is not None
        else start_at_utc + timedelta(minutes=duration_minutes)
    )
    return RequestedInterval(start_at_utc, end_at_utc)


def _requirements_for(
    service: ServiceSpec | ServiceCatalogView,
) -> Sequence[BookingRequirementSpec]:
    return service.booking_requirements


def validate_required_booking_details(
    service: ServiceSpec | ServiceCatalogView, details: Mapping[str, str]
) -> None:
    """Validate only the configured, bounded service requirement values."""
    if len(details) > 16:
        raise ValueError("booking details must be bounded")
    requirements = tuple(_requirements_for(service))
    configured = {requirement.key: requirement for requirement in requirements}
    unknown = set(details) - set(configured)
    if unknown:
        raise ValueError("unknown requirement key")
    for key, value in details.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("booking requirement keys must not be blank")
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_DETAIL_LENGTH:
            raise ValueError("booking requirement values must be bounded")
    missing = {
        requirement.key
        for requirement in requirements
        if requirement.required and requirement.key not in details
    }
    if missing:
        raise ValueError("required requirement is missing")


def match_catalog_selector(
    selector: str, labels_by_code: Mapping[str, Sequence[str]]
) -> str | None:
    """Return one exact normalized catalog code, or ``None`` when unknown."""
    normalized_selector = normalize_catalog_text(selector)
    if not normalized_selector:
        return None
    matches = {
        code
        for code, labels in labels_by_code.items()
        if normalized_selector
        in {normalize_catalog_text(code), *(normalize_catalog_text(label) for label in labels)}
    }
    if len(matches) > 1:
        raise CatalogSelectorAmbiguous("catalog selector is ambiguous")
    return next(iter(matches), None)


def validate_service_eligibility(service: ServiceSpec) -> None:
    """Reject inactive or non-bookable services with stable decision labels."""
    if not service.active:
        raise ValueError("service_inactive")
    if not service.bookable:
        raise ValueError("service_not_bookable")


def validate_service_area_eligibility(area: ServiceAreaSpec) -> None:
    """Reject inactive service areas with the stable decision label."""
    if not area.active:
        raise ValueError("unsupported_service_area")


def _business_config_parts(
    config: BusinessConfigSpec | BusinessConfigView,
) -> tuple[str, WeeklyHours, BookingPolicySpec]:
    return config.timezone, config.weekly_hours, config.booking_policy


def _policy_failure(interval: RequestedInterval) -> PolicyDecision:
    return PolicyDecision(
        valid=False,
        error_code=_OUTSIDE_BUSINESS_POLICY,
        effective_interval=interval,
    )


def evaluate_booking_policy(
    config: BusinessConfigSpec | BusinessConfigView,
    interval: RequestedInterval,
    now_utc: datetime,
) -> PolicyDecision:
    """Evaluate the configured local business policy without side effects."""
    now_at_utc = _normalize_aware_instant(now_utc)
    timezone_name, weekly_hours, policy = _business_config_parts(config)
    business_timezone = ZoneInfo(timezone_name)
    local_start = interval.start_at_utc.astimezone(business_timezone)
    local_end = interval.end_at_utc.astimezone(business_timezone)

    if local_start.date() != local_end.date():
        return _policy_failure(interval)

    effective_interval = RequestedInterval(
        interval.start_at_utc - timedelta(minutes=policy.buffer_before_minutes),
        interval.end_at_utc + timedelta(minutes=policy.buffer_after_minutes),
    )
    effective_start = effective_interval.start_at_utc.astimezone(business_timezone)
    effective_end = effective_interval.end_at_utc.astimezone(business_timezone)
    if effective_start.date() != local_start.date() or effective_end.date() != local_start.date():
        return _policy_failure(effective_interval)

    try:
        weekday = Weekday(local_start.strftime("%A").lower())
    except ValueError:
        return _policy_failure(effective_interval)
    windows = weekly_hours.days.get(weekday, [])
    fits_opening_window = any(
        effective_start.timetz().replace(tzinfo=None) >= window.start
        and effective_end.timetz().replace(tzinfo=None) <= window.end
        for window in windows
    )
    if not fits_opening_window:
        return _policy_failure(effective_interval)

    local_midnight = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_from_midnight = int((local_start - local_midnight).total_seconds() // 60)
    if (
        local_start.second != 0
        or local_start.microsecond != 0
        or minutes_from_midnight % policy.slot_increment_minutes != 0
    ):
        return _policy_failure(effective_interval)

    if interval.start_at_utc < now_at_utc + timedelta(minutes=policy.minimum_notice_minutes):
        return _policy_failure(effective_interval)

    current_local_date = now_at_utc.astimezone(business_timezone).date()
    if not (
        current_local_date
        <= local_start.date()
        <= current_local_date + timedelta(days=policy.maximum_advance_days)
    ):
        return _policy_failure(effective_interval)

    return PolicyDecision(valid=True, error_code=None, effective_interval=effective_interval)


def intervals_overlap(left: RequestedInterval, right: CalendarInterval) -> bool:
    """Use half-open interval semantics for provider conflict decisions."""
    return left.start_at_utc < right.end_at_utc and right.start_at_utc < left.end_at_utc


def decide_availability(
    *,
    policy_valid: bool,
    effective_interval: RequestedInterval,
    provider_intervals: tuple[CalendarInterval, ...],
    policy_error_code: str | None = None,
) -> AvailabilityDecision:
    """Return availability only from policy validity and provider intervals."""
    if not policy_valid:
        return AvailabilityDecision(
            policy_valid=False,
            provider_available=False,
            error_code=policy_error_code or _OUTSIDE_BUSINESS_POLICY,
            effective_interval=effective_interval,
        )
    return AvailabilityDecision(
        policy_valid=True,
        provider_available=not any(
            intervals_overlap(effective_interval, provider_interval)
            for provider_interval in provider_intervals
        ),
        error_code=None,
        effective_interval=effective_interval,
    )


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalize_aware_instant(value).isoformat().replace("+00:00", "Z")


def _canonical_identifier(value: UUID | str) -> str:
    return str(value)


def canonical_booking_snapshot(
    *,
    booking_id: UUID | str,
    service_id: UUID | str,
    status: BookingStatus | str,
    start_at_utc: datetime | None,
    end_at_utc: datetime | None,
    calendar_id: str | None,
    calendar_event_id: str | None,
    request_data: Mapping[str, object],
) -> dict[str, object]:
    """Build the exact canonical fields used for expected-state comparison."""
    if (start_at_utc is None) != (end_at_utc is None):
        raise ValueError("booking snapshot interval must contain both timestamps or neither")
    try:
        request_json = json.dumps(
            request_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("request data must be JSON serializable") from exc
    status_value = status.value if isinstance(status, StrEnum) else str(status)
    return {
        "booking_id": _canonical_identifier(booking_id),
        "service_id": _canonical_identifier(service_id),
        "status": status_value,
        "start_at_utc": _canonical_datetime(start_at_utc),
        "end_at_utc": _canonical_datetime(end_at_utc),
        "calendar_id": calendar_id,
        "calendar_event_id": calendar_event_id,
        "request_data_sha256": hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
    }


def booking_state_fingerprint(snapshot: Mapping[str, object]) -> str:
    """Hash exactly one canonical Booking snapshot shape."""
    expected_keys = {
        "booking_id",
        "service_id",
        "status",
        "start_at_utc",
        "end_at_utc",
        "calendar_id",
        "calendar_event_id",
        "request_data_sha256",
    }
    if set(snapshot) != expected_keys:
        raise ValueError("booking snapshot fields are not canonical")
    try:
        canonical_json = json.dumps(
            dict(snapshot),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("booking snapshot must be JSON serializable") from exc
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

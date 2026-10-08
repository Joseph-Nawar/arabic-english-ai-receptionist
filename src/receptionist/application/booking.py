"""Strict contracts for the finite Phase 2 booking operation surface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from receptionist.db.models import ToolExecution
from receptionist.domain.booking_policy import (
    AvailabilityDecision,
    PolicyDecision,
    RequestedInterval,
    booking_state_fingerprint,
    canonical_booking_snapshot,
    decide_availability,
    intervals_overlap,
)
from receptionist.domain.enums import (
    BookingStatus,
    PendingActionStatus,
    PendingActionType,
    ToolExecutionStatus,
)
from receptionist.domain.state import PendingActionState
from receptionist.integrations.google_calendar import (
    CalendarClient,
    CalendarClientError,
)

__all__ = [
    "AvailabilityDecision",
    "PolicyDecision",
    "RequestedInterval",
    "booking_state_fingerprint",
    "canonical_booking_snapshot",
    "decide_availability",
    "intervals_overlap",
]

_MAX_DETAIL_KEYS = 16
_FORBIDDEN_DETAIL_KEYS = {
    "address",
    "authorization",
    "body",
    "credential",
    "credentials",
    "email",
    "etag",
    "headers",
    "payload",
    "phone",
    "provider_payload",
    "raw",
    "raw_payload",
    "transcript",
}

type SafeScalar = str | int | bool
type AwareDateTime = Annotated[datetime, AwareDatetime]


class _BookingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


def generate_action_token() -> str:
    """Return a fresh opaque token for one prepared booking action."""
    return str(uuid4())


def pending_action_matches(
    current: PendingActionState | None,
    expected_type: PendingActionType,
    action_token: str,
) -> bool:
    """Require the exact current awaiting action type and token."""
    return (
        current is not None
        and current.status is PendingActionStatus.AWAITING_CONFIRMATION
        and current.type is expected_type
        and current.payload.get("action_token") == action_token
    )


def _validate_safe_mapping(values: Mapping[str, SafeScalar]) -> dict[str, SafeScalar]:
    if len(values) > _MAX_DETAIL_KEYS:
        raise ValueError(f"at most {_MAX_DETAIL_KEYS} detail fields are allowed")
    safe_values: dict[str, SafeScalar] = {}
    for key, value in values.items():
        if not key.strip() or len(key) > 80:
            raise ValueError("detail keys must be non-blank and bounded")
        if key.casefold() in _FORBIDDEN_DETAIL_KEYS:
            raise ValueError("detail key is not safe for durable metadata")
        if isinstance(value, str) and len(value) > 500:
            raise ValueError("detail values must be bounded")
        safe_values[key] = value
    return safe_values


class ServiceLookupRequest(_BookingModel):
    selector: str = Field(min_length=1, max_length=200)

    _selector_not_blank = field_validator("selector")(_require_non_blank)


class ServiceAreaLookupRequest(_BookingModel):
    selector: str = Field(min_length=1, max_length=200)

    _selector_not_blank = field_validator("selector")(_require_non_blank)


class AvailabilityRequest(_BookingModel):
    service_selector: str = Field(min_length=1, max_length=200)
    requested_start_at: AwareDateTime
    requested_end_at: AwareDateTime | None = None

    _selector_not_blank = field_validator("service_selector")(_require_non_blank)


class GetBookingRequest(_BookingModel):
    conversation_id: UUID
    booking_id: UUID


class CreateBookingRequest(_BookingModel):
    conversation_id: UUID
    service_selector: str = Field(min_length=1, max_length=200)
    service_area_selector: str = Field(min_length=1, max_length=200)
    requested_start_at: AwareDateTime
    requested_end_at: AwareDateTime | None = None
    requirements: dict[str, str] = Field(default_factory=dict, max_length=_MAX_DETAIL_KEYS)
    idempotency_key: str = Field(min_length=1, max_length=255)

    _selectors_not_blank = field_validator("service_selector", "service_area_selector")(
        _require_non_blank
    )
    _idempotency_not_blank = field_validator("idempotency_key")(_require_non_blank)

    @field_validator("requirements")
    @classmethod
    def validate_requirements(cls, values: dict[str, str]) -> dict[str, str]:
        safe_values = _validate_safe_mapping(values)
        return {key: value for key, value in safe_values.items() if isinstance(value, str)}


class RescheduleBookingRequest(_BookingModel):
    conversation_id: UUID
    booking_id: UUID
    requested_start_at: AwareDateTime
    requested_end_at: AwareDateTime | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)

    _idempotency_not_blank = field_validator("idempotency_key")(_require_non_blank)


class CancelBookingRequest(_BookingModel):
    conversation_id: UUID
    booking_id: UUID
    cancellation_context: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=255)

    _idempotency_not_blank = field_validator("idempotency_key")(_require_non_blank)

    @field_validator("cancellation_context")
    @classmethod
    def validate_cancellation_context(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("cancellation_context must not be blank when supplied")
        return value


class ConfirmBookingActionRequest(_BookingModel):
    conversation_id: UUID
    action_type: PendingActionType
    action_token: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)

    _action_token_not_blank = field_validator("action_token")(_require_non_blank)
    _idempotency_not_blank = field_validator("idempotency_key")(_require_non_blank)


class BookingErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNKNOWN_SERVICE = "unknown_service"
    SERVICE_INACTIVE = "service_inactive"
    SERVICE_NOT_BOOKABLE = "service_not_bookable"
    UNSUPPORTED_SERVICE_AREA = "unsupported_service_area"
    CONFIGURATION_CONFLICT = "configuration_conflict"
    INVALID_REQUESTED_TIME = "invalid_requested_time"
    OUTSIDE_BUSINESS_POLICY = "outside_business_policy"
    CONFIRMATION_REQUIRED = "confirmation_required"
    PENDING_ACTION_CONFLICT = "pending_action_conflict"
    STALE_PENDING_ACTION = "stale_pending_action"
    BOOKING_NOT_FOUND = "booking_not_found"
    BOOKING_STATE_CONFLICT = "booking_state_conflict"
    EXTERNAL_REFERENCE_MISSING = "external_reference_missing"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DUPLICATE_REPLAY = "duplicate_replay"
    CALENDAR_INTERVAL_UNAVAILABLE = "calendar_interval_unavailable"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    EXTERNAL_EVENT_MISSING = "external_event_missing"
    EXTERNAL_STATE_CONFLICT = "external_state_conflict"
    CALENDAR_RECONCILIATION_REQUIRED = "calendar_reconciliation_required"


class ToolExecutionClaimOutcome(StrEnum):
    NEW = "new"
    SUCCEEDED_REPLAY = "succeeded_replay"
    REJECTED_REPLAY = "rejected_replay"
    FAILED_REPLAY = "failed_replay"
    STARTED_RECOVERY = "started_recovery"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_IN_PROGRESS = "operation_in_progress"


@dataclass(frozen=True, slots=True)
class ToolExecutionClaim:
    """The explicit claim/replay outcome for one booking operation key."""

    outcome: ToolExecutionClaimOutcome
    execution: ToolExecution


class BookingError(_BookingModel):
    code: BookingErrorCode
    message: str = Field(min_length=1, max_length=300)
    retryable: bool
    details: dict[str, SafeScalar] = Field(default_factory=dict, max_length=_MAX_DETAIL_KEYS)

    _message_not_blank = field_validator("message")(_require_non_blank)

    @field_validator("details")
    @classmethod
    def validate_details(cls, values: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _validate_safe_mapping(values)


class SanitizedMetadata(_BookingModel):
    values: dict[str, SafeScalar] = Field(default_factory=dict, max_length=_MAX_DETAIL_KEYS)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _validate_safe_mapping(values)


class ToolExecutionMetadata(_BookingModel):
    operation: str = Field(min_length=1, max_length=80)
    arguments: SanitizedMetadata = Field(default_factory=SanitizedMetadata)
    result: SanitizedMetadata | None = None
    error_code: BookingErrorCode | None = None

    _operation_not_blank = field_validator("operation")(_require_non_blank)


class AuditEventMetadata(_BookingModel):
    operation: str = Field(min_length=1, max_length=80)
    outcome: Literal["succeeded", "rejected", "reconciled"]
    entity_id: UUID | None = None
    details: SanitizedMetadata = Field(default_factory=SanitizedMetadata)

    _operation_not_blank = field_validator("operation")(_require_non_blank)


class ServiceLookupData(_BookingModel):
    code: str
    name_en: str
    name_ar: str
    active: bool
    bookable: bool
    duration_minutes: int = Field(gt=0)
    pricing: SanitizedMetadata = Field(default_factory=SanitizedMetadata)
    booking_requirements: tuple[str, ...] = ()


class ServiceAreaLookupData(_BookingModel):
    code: str
    name_en: str
    name_ar: str
    active: bool


class AvailabilityData(_BookingModel):
    requested_start_at_utc: AwareDateTime
    requested_end_at_utc: AwareDateTime
    policy_valid: bool
    provider_available: bool
    read_at: AwareDateTime


class BookingReadData(_BookingModel):
    booking_id: UUID
    status: BookingStatus
    start_at_utc: AwareDateTime | None = None
    end_at_utc: AwareDateTime | None = None
    calendar_id: str | None = None
    calendar_event_id: str | None = None
    reconciliation_status: Literal[
        "in_sync",
        "provider_divergent",
        "provider_missing",
        "operation_in_progress",
    ]


class PreparationData(_BookingModel):
    action_type: PendingActionType
    action_token: str = Field(min_length=1, max_length=128)
    confirmation_required: Literal[True] = True
    requested_start_at_utc: AwareDateTime
    requested_end_at_utc: AwareDateTime

    _action_token_not_blank = field_validator("action_token")(_require_non_blank)


class ConfirmationData(_BookingModel):
    booking_id: UUID
    status: BookingStatus
    requested_start_at_utc: AwareDateTime | None = None
    requested_end_at_utc: AwareDateTime | None = None
    calendar_id: str | None = None
    calendar_event_id: str | None = None


class ServiceLookupResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal["lookup_service"] = "lookup_service"
    replayed: bool = False
    data: ServiceLookupData


class ServiceAreaLookupResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal["lookup_service_area"] = "lookup_service_area"
    replayed: bool = False
    data: ServiceAreaLookupData


class AvailabilityResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal["check_availability"] = "check_availability"
    replayed: bool = False
    data: AvailabilityData


class BookingReadResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal["get_booking"] = "get_booking"
    replayed: bool = False
    data: BookingReadData


class PreparationResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal[
        "prepare_create_booking",
        "prepare_reschedule_booking",
        "prepare_cancel_booking",
    ]
    replayed: bool = False
    data: PreparationData


class ConfirmationResult(_BookingModel):
    ok: Literal[True] = True
    operation: Literal["confirm_booking_action"] = "confirm_booking_action"
    replayed: bool = False
    data: ConfirmationData


class BookingErrorResult(_BookingModel):
    ok: Literal[False] = False
    operation: str = Field(min_length=1, max_length=80)
    error: BookingError

    _operation_not_blank = field_validator("operation")(_require_non_blank)


def _arguments_fingerprint(arguments: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("sanitized arguments must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _claim_matches(
    execution: ToolExecution,
    *,
    conversation_id: UUID,
    tool_name: str,
    arguments_fingerprint: str,
) -> bool:
    return (
        execution.conversation_id == conversation_id
        and execution.tool_name == tool_name
        and _arguments_fingerprint(execution.sanitized_arguments) == arguments_fingerprint
    )


async def _claim_tool_execution(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    tool_name: str,
    idempotency_key: str,
    sanitized_arguments: dict[str, object],
) -> ToolExecutionClaim:
    """Claim one booking execution key with PostgreSQL conflict-safe insertion."""
    arguments_fingerprint = _arguments_fingerprint(sanitized_arguments)
    active_execution = await session.scalar(
        select(ToolExecution)
        .where(
            ToolExecution.conversation_id == conversation_id,
            ToolExecution.tool_name == tool_name,
            ToolExecution.status == ToolExecutionStatus.STARTED,
            ToolExecution.idempotency_key != idempotency_key,
        )
        .with_for_update()
    )
    if active_execution is not None:
        return ToolExecutionClaim(ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS, active_execution)

    statement = (
        pg_insert(ToolExecution)
        .values(
            conversation_id=conversation_id,
            tool_name=tool_name,
            status=ToolExecutionStatus.STARTED,
            idempotency_key=idempotency_key,
            sanitized_arguments=sanitized_arguments,
        )
        .on_conflict_do_nothing()
        .returning(ToolExecution)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        return ToolExecutionClaim(ToolExecutionClaimOutcome.NEW, inserted)

    existing = await session.scalar(
        select(ToolExecution)
        .where(ToolExecution.idempotency_key == idempotency_key)
        .with_for_update()
    )
    if existing is None:
        raise RuntimeError("idempotency claim disappeared after conflict")
    if not _claim_matches(
        existing,
        conversation_id=conversation_id,
        tool_name=tool_name,
        arguments_fingerprint=arguments_fingerprint,
    ):
        return ToolExecutionClaim(ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT, existing)
    outcomes = {
        ToolExecutionStatus.SUCCEEDED: ToolExecutionClaimOutcome.SUCCEEDED_REPLAY,
        ToolExecutionStatus.REJECTED: ToolExecutionClaimOutcome.REJECTED_REPLAY,
        ToolExecutionStatus.FAILED: ToolExecutionClaimOutcome.FAILED_REPLAY,
        ToolExecutionStatus.STARTED: ToolExecutionClaimOutcome.STARTED_RECOVERY,
    }
    return ToolExecutionClaim(outcomes[existing.status], existing)


async def check_calendar_availability(
    calendar: CalendarClient,
    *,
    calendar_id: str,
    policy: PolicyDecision,
    exclude_event_id: str | None = None,
) -> AvailabilityDecision:
    """Compose policy validity with the appropriate provider availability read."""
    if not policy.valid:
        return decide_availability(
            policy_valid=False,
            effective_interval=policy.effective_interval,
            provider_intervals=(),
            policy_error_code=policy.error_code,
        )

    effective_interval = policy.effective_interval
    try:
        if exclude_event_id is None:
            busy_intervals = await calendar.query_free_busy(
                calendar_id,
                effective_interval.start_at_utc,
                effective_interval.end_at_utc,
            )
            provider_intervals = tuple(
                RequestedInterval(interval.start_at_utc, interval.end_at_utc)
                for interval in busy_intervals
            )
        else:
            conflicts = await calendar.query_conflicts(
                calendar_id,
                effective_interval.start_at_utc,
                effective_interval.end_at_utc,
                exclude_event_id=exclude_event_id,
            )
            provider_intervals = tuple(
                RequestedInterval(
                    conflict.interval.start_at_utc,
                    conflict.interval.end_at_utc,
                )
                for conflict in conflicts
            )
    except CalendarClientError as exc:
        return AvailabilityDecision(
            policy_valid=True,
            provider_available=False,
            error_code=exc.code.value,
            effective_interval=effective_interval,
        )

    return decide_availability(
        policy_valid=True,
        effective_interval=effective_interval,
        provider_intervals=provider_intervals,
    )

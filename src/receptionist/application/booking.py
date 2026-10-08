"""Strict contracts for the finite Phase 2 booking operation surface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.db.models import (
    AuditEvent,
    Booking,
    BusinessConfig,
    Conversation,
    Service,
    ToolExecution,
)
from receptionist.domain.booking_policy import (
    AvailabilityDecision,
    CatalogSelectorAmbiguous,
    PolicyDecision,
    RequestedInterval,
    ServiceCatalogView,
    booking_state_fingerprint,
    canonical_booking_snapshot,
    decide_availability,
    evaluate_booking_policy,
    intervals_overlap,
    match_catalog_selector,
    normalize_requested_interval,
    validate_required_booking_details,
)
from receptionist.domain.config import (
    BookingRequirementSpec,
    BusinessConfigSpec,
    ServiceAreaSpec,
)
from receptionist.domain.enums import (
    AuditActorType,
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


def _claim_existing_execution(
    execution: ToolExecution,
    *,
    conversation_id: UUID,
    tool_name: str,
    arguments_fingerprint: str,
) -> ToolExecutionClaim:
    if not _claim_matches(
        execution,
        conversation_id=conversation_id,
        tool_name=tool_name,
        arguments_fingerprint=arguments_fingerprint,
    ):
        return ToolExecutionClaim(ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT, execution)
    outcomes = {
        ToolExecutionStatus.SUCCEEDED: ToolExecutionClaimOutcome.SUCCEEDED_REPLAY,
        ToolExecutionStatus.REJECTED: ToolExecutionClaimOutcome.REJECTED_REPLAY,
        ToolExecutionStatus.FAILED: ToolExecutionClaimOutcome.FAILED_REPLAY,
        ToolExecutionStatus.STARTED: ToolExecutionClaimOutcome.STARTED_RECOVERY,
    }
    return ToolExecutionClaim(outcomes[execution.status], execution)


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
    existing = await session.scalar(
        select(ToolExecution)
        .where(ToolExecution.idempotency_key == idempotency_key)
        .with_for_update()
    )
    if existing is not None:
        return _claim_existing_execution(
            existing,
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments_fingerprint=arguments_fingerprint,
        )

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
    return _claim_existing_execution(
        existing,
        conversation_id=conversation_id,
        tool_name=tool_name,
        arguments_fingerprint=arguments_fingerprint,
    )


_ERROR_MESSAGES = {
    BookingErrorCode.INVALID_INPUT: "the booking request is invalid",
    BookingErrorCode.UNKNOWN_SERVICE: "the requested service was not found",
    BookingErrorCode.SERVICE_INACTIVE: "the requested service is inactive",
    BookingErrorCode.SERVICE_NOT_BOOKABLE: "the requested service is not bookable",
    BookingErrorCode.UNSUPPORTED_SERVICE_AREA: "the requested service area is unsupported",
    BookingErrorCode.CONFIGURATION_CONFLICT: "booking configuration is unavailable",
    BookingErrorCode.INVALID_REQUESTED_TIME: "the requested time is invalid",
    BookingErrorCode.OUTSIDE_BUSINESS_POLICY: "the requested time is outside business policy",
    BookingErrorCode.CONFIRMATION_REQUIRED: "confirmation is required",
    BookingErrorCode.PENDING_ACTION_CONFLICT: "another booking action is already pending",
    BookingErrorCode.STALE_PENDING_ACTION: "the booking action is stale",
    BookingErrorCode.BOOKING_NOT_FOUND: "the booking was not found",
    BookingErrorCode.BOOKING_STATE_CONFLICT: "the booking is not in a supported state",
    BookingErrorCode.EXTERNAL_REFERENCE_MISSING: "the booking has no managed Calendar reference",
    BookingErrorCode.OPERATION_IN_PROGRESS: "another booking operation is in progress",
    BookingErrorCode.IDEMPOTENCY_CONFLICT: "the idempotency key belongs to another operation",
    BookingErrorCode.DUPLICATE_REPLAY: "the booking operation is a duplicate",
    BookingErrorCode.CALENDAR_INTERVAL_UNAVAILABLE: "the requested Calendar interval is busy",
    BookingErrorCode.CALENDAR_UNAVAILABLE: "the Calendar is unavailable",
    BookingErrorCode.EXTERNAL_EVENT_MISSING: "the Calendar event is missing",
    BookingErrorCode.EXTERNAL_STATE_CONFLICT: "the Calendar state changed",
    BookingErrorCode.CALENDAR_RECONCILIATION_REQUIRED: "Calendar reconciliation is required",
}


def _booking_error_result(
    operation: str,
    code: BookingErrorCode,
    *,
    retryable: bool = False,
    details: dict[str, SafeScalar] | None = None,
) -> BookingErrorResult:
    return BookingErrorResult(
        operation=operation,
        error=BookingError(
            code=code,
            message=_ERROR_MESSAGES[code],
            retryable=retryable,
            details=details or {},
        ),
    )


def _replay_claim(
    claim: ToolExecutionClaim,
    operation: str,
) -> PreparationResult | BookingErrorResult | None:
    if claim.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT:
        return _booking_error_result(operation, BookingErrorCode.IDEMPOTENCY_CONFLICT)
    if claim.outcome is ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS:
        return _booking_error_result(
            operation, BookingErrorCode.OPERATION_IN_PROGRESS, retryable=True
        )
    if claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY:
        return _booking_error_result(
            operation, BookingErrorCode.OPERATION_IN_PROGRESS, retryable=True
        )
    if claim.outcome is ToolExecutionClaimOutcome.NEW:
        return None
    stored = claim.execution.sanitized_result
    if not isinstance(stored, Mapping):
        return _booking_error_result(
            operation, BookingErrorCode.OPERATION_IN_PROGRESS, retryable=True
        )
    if stored.get("ok") is True:
        result = PreparationResult.model_validate(stored)
        return result.model_copy(update={"replayed": True})
    return BookingErrorResult.model_validate(stored)


def _utc_storage_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operation time must be timezone-aware")
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _utc_storage_time(value).isoformat().replace("+00:00", "Z")


def _business_spec(row: BusinessConfig) -> BusinessConfigSpec:
    return BusinessConfigSpec.model_validate_json(
        json.dumps(
            {
                "business_name": row.business_name,
                "timezone": row.timezone,
                "default_phone_region": row.default_phone_region,
                "default_currency": row.default_currency,
                "supported_languages": row.supported_languages,
                "default_language": row.default_language,
                "weekly_hours": row.weekly_hours,
                "after_hours_policy": row.after_hours_policy,
                "service_areas": row.service_areas,
                "booking_policy": row.booking_policy,
                "handoff_policy": row.handoff_policy,
                "greeting_templates": row.greeting_templates,
                "closing_templates": row.closing_templates,
                "retention_policy": row.retention_policy,
            }
        )
    )


def _service_requirements(row: Service) -> ServiceCatalogView:
    return ServiceCatalogView(
        booking_requirements=tuple(
            BookingRequirementSpec.model_validate(value) for value in row.booking_requirements
        )
    )


async def _resolve_service(
    session: AsyncSession, selector: str
) -> tuple[Service | None, BookingErrorCode | None]:
    rows = (await session.scalars(select(Service).order_by(Service.code))).all()
    labels = {row.code: [row.name_en, row.name_ar, *row.aliases] for row in rows}
    try:
        code = match_catalog_selector(selector, labels)
    except CatalogSelectorAmbiguous:
        return None, BookingErrorCode.CONFIGURATION_CONFLICT
    if code is None:
        return None, BookingErrorCode.UNKNOWN_SERVICE
    row = next(row for row in rows if row.code == code)
    if not row.active:
        return row, BookingErrorCode.SERVICE_INACTIVE
    if not row.bookable:
        return row, BookingErrorCode.SERVICE_NOT_BOOKABLE
    return row, None


def _resolve_area(
    config: BusinessConfigSpec, selector: str
) -> tuple[ServiceAreaSpec | None, BookingErrorCode | None]:
    labels = {
        area.code: [area.name_en, area.name_ar, *area.aliases] for area in config.service_areas
    }
    try:
        code = match_catalog_selector(selector, labels)
    except CatalogSelectorAmbiguous:
        return None, BookingErrorCode.CONFIGURATION_CONFLICT
    if code is None:
        return None, BookingErrorCode.UNSUPPORTED_SERVICE_AREA
    area = next(area for area in config.service_areas if area.code == code)
    if not area.active:
        return area, BookingErrorCode.UNSUPPORTED_SERVICE_AREA
    return area, None


def _calendar_id(calendar: CalendarClient) -> str | None:
    value = calendar.calendar_id
    return value if value.strip() else None


def _policy_error(decision: PolicyDecision) -> BookingErrorCode | None:
    if decision.valid:
        return None
    try:
        return BookingErrorCode(decision.error_code or BookingErrorCode.OUTSIDE_BUSINESS_POLICY)
    except ValueError:
        return BookingErrorCode.OUTSIDE_BUSINESS_POLICY


def _availability_error(decision: AvailabilityDecision) -> BookingErrorCode | None:
    if decision.provider_available:
        return None
    if decision.error_code is None:
        return BookingErrorCode.CALENDAR_INTERVAL_UNAVAILABLE
    try:
        return BookingErrorCode(decision.error_code)
    except ValueError:
        return BookingErrorCode.CALENDAR_UNAVAILABLE


def _safe_cancellation_context(value: str | None) -> str | None:
    if value is None:
        return None
    return "provided" if value.strip() else None


def _pending_payload(
    *,
    action_type: PendingActionType,
    action_token: str,
    fields: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": action_type.value,
        "action_token": action_token,
        **fields,
    }


def _stage_pending_action(
    conversation: Conversation,
    *,
    action_type: PendingActionType,
    payload: dict[str, object],
    created_at: datetime,
) -> None:
    conversation.pending_action_type = action_type
    conversation.pending_action_status = PendingActionStatus.AWAITING_CONFIRMATION
    conversation.pending_action_payload = payload
    conversation.pending_action_created_at = created_at
    conversation.pending_action_confirmed_at = None
    conversation.pending_action_expires_at = None


def _terminalize_execution(
    session: AsyncSession,
    *,
    conversation: Conversation,
    execution: ToolExecution,
    result: PreparationResult | BookingErrorResult,
    finished_at: datetime,
) -> None:
    serialized = result.model_dump(mode="json")
    execution.status = (
        ToolExecutionStatus.SUCCEEDED
        if isinstance(result, PreparationResult)
        else ToolExecutionStatus.REJECTED
    )
    execution.sanitized_result = serialized
    execution.finished_at = finished_at
    if isinstance(result, BookingErrorResult):
        execution.safe_error_code = result.error.code.value
        execution.safe_error_message = result.error.message
        outcome = "rejected"
    else:
        execution.safe_error_code = None
        execution.safe_error_message = None
        outcome = "succeeded"
    session.add(
        AuditEvent(
            event_type=execution.tool_name,
            actor_type=AuditActorType.SYSTEM,
            contact_id=conversation.contact_id,
            conversation_id=conversation.id,
            entity_type="conversation",
            entity_identifier=str(conversation.id),
            sanitized_metadata={
                "operation": execution.tool_name,
                "outcome": outcome,
                "tool_execution_id": str(execution.id),
            },
        )
    )


def _request_arguments(request: CreateBookingRequest) -> dict[str, object]:
    return {
        "conversation_id": str(request.conversation_id),
        "service_selector": request.service_selector,
        "service_area_selector": request.service_area_selector,
        "requested_start_at": _iso_utc(request.requested_start_at),
        "requested_end_at": _iso_utc(request.requested_end_at)
        if request.requested_end_at is not None
        else None,
        "requirements": dict(request.requirements),
    }


def _reschedule_arguments(request: RescheduleBookingRequest) -> dict[str, object]:
    return {
        "conversation_id": str(request.conversation_id),
        "booking_id": str(request.booking_id),
        "requested_start_at": _iso_utc(request.requested_start_at),
        "requested_end_at": _iso_utc(request.requested_end_at)
        if request.requested_end_at is not None
        else None,
    }


def _cancel_arguments(request: CancelBookingRequest) -> dict[str, object]:
    return {
        "conversation_id": str(request.conversation_id),
        "booking_id": str(request.booking_id),
        "cancellation_context": _safe_cancellation_context(request.cancellation_context),
    }


async def prepare_create_booking(
    session_factory: async_sessionmaker[AsyncSession],
    calendar: CalendarClient,
    request: CreateBookingRequest,
    *,
    now_utc: datetime,
) -> PreparationResult | BookingErrorResult:
    """Validate and stage a create action without creating local or provider state."""
    operation: Literal["prepare_create_booking"] = "prepare_create_booking"
    result: PreparationResult | BookingErrorResult
    now = _utc_storage_time(now_utc)
    arguments = _request_arguments(request)
    async with session_factory.begin() as session:
        conversation = await session.get(Conversation, request.conversation_id)
        if conversation is None:
            return _booking_error_result(operation, BookingErrorCode.INVALID_INPUT)
        claim = await _claim_tool_execution(
            session,
            conversation_id=request.conversation_id,
            tool_name=operation,
            idempotency_key=request.idempotency_key,
            sanitized_arguments=arguments,
        )
        replay = _replay_claim(claim, operation)
        if replay is not None:
            return replay
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == request.conversation_id).with_for_update()
        )
        if conversation is None:
            raise RuntimeError("conversation disappeared while locked")
        if conversation.pending_action_type is not None:
            result = _booking_error_result(operation, BookingErrorCode.PENDING_ACTION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result

        business_row = await session.get(BusinessConfig, 1)
        if business_row is None:
            result = _booking_error_result(operation, BookingErrorCode.CONFIGURATION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        try:
            business = _business_spec(business_row)
        except ValueError:
            result = _booking_error_result(operation, BookingErrorCode.CONFIGURATION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result

        service, service_error = await _resolve_service(session, request.service_selector)
        if service_error is not None or service is None:
            result = _booking_error_result(
                operation, service_error or BookingErrorCode.UNKNOWN_SERVICE
            )
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        area, area_error = _resolve_area(business, request.service_area_selector)
        if area_error is not None or area is None:
            result = _booking_error_result(
                operation, area_error or BookingErrorCode.UNSUPPORTED_SERVICE_AREA
            )
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        try:
            service_view = _service_requirements(service)
            validate_required_booking_details(service_view, request.requirements)
        except ValueError:
            result = _booking_error_result(operation, BookingErrorCode.INVALID_INPUT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        try:
            interval = normalize_requested_interval(
                request.requested_start_at,
                request.requested_end_at,
                service.default_duration_minutes,
            )
            policy = evaluate_booking_policy(business, interval, now)
        except ValueError:
            result = _booking_error_result(operation, BookingErrorCode.INVALID_REQUESTED_TIME)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        policy_error = _policy_error(policy)
        if policy_error is not None:
            result = _booking_error_result(operation, policy_error)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        configured_calendar_id = _calendar_id(calendar)
        if configured_calendar_id is None:
            result = _booking_error_result(operation, BookingErrorCode.CONFIGURATION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        availability = await check_calendar_availability(
            calendar,
            calendar_id=configured_calendar_id,
            policy=policy,
        )
        availability_error = _availability_error(availability)
        if availability_error is not None:
            result = _booking_error_result(
                operation,
                availability_error,
                retryable=availability_error is BookingErrorCode.CALENDAR_UNAVAILABLE,
            )
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result

        action_token = generate_action_token()
        payload = _pending_payload(
            action_type=PendingActionType.CREATE_BOOKING,
            action_token=action_token,
            fields={
                "service_code": service.code,
                "service_area_code": area.code,
                "requested_start_at_utc": _iso_utc(interval.start_at_utc),
                "requested_end_at_utc": _iso_utc(interval.end_at_utc),
                "requirements": dict(request.requirements),
            },
        )
        _stage_pending_action(
            conversation,
            action_type=PendingActionType.CREATE_BOOKING,
            payload=payload,
            created_at=now,
        )
        result = PreparationResult(
            operation=operation,
            data=PreparationData(
                action_type=PendingActionType.CREATE_BOOKING,
                action_token=action_token,
                requested_start_at_utc=interval.start_at_utc,
                requested_end_at_utc=interval.end_at_utc,
            ),
        )
        _terminalize_execution(
            session,
            conversation=conversation,
            execution=claim.execution,
            result=result,
            finished_at=now,
        )
        return result


def _expected_booking_fingerprint(booking: Booking) -> str:
    snapshot = canonical_booking_snapshot(
        booking_id=booking.id,
        service_id=booking.service_id,
        status=booking.status,
        start_at_utc=booking.start_at,
        end_at_utc=booking.end_at,
        calendar_id=booking.calendar_id,
        calendar_event_id=booking.calendar_event_id,
        request_data=booking.booking_data,
    )
    return booking_state_fingerprint(snapshot)


async def prepare_reschedule_booking(
    session_factory: async_sessionmaker[AsyncSession],
    calendar: CalendarClient,
    request: RescheduleBookingRequest,
    *,
    now_utc: datetime,
) -> PreparationResult | BookingErrorResult:
    """Validate and stage a reschedule action without changing the Booking."""
    operation: Literal["prepare_reschedule_booking"] = "prepare_reschedule_booking"
    result: PreparationResult | BookingErrorResult
    now = _utc_storage_time(now_utc)
    arguments = _reschedule_arguments(request)
    async with session_factory.begin() as session:
        conversation = await session.get(Conversation, request.conversation_id)
        if conversation is None:
            return _booking_error_result(operation, BookingErrorCode.INVALID_INPUT)
        claim = await _claim_tool_execution(
            session,
            conversation_id=request.conversation_id,
            tool_name=operation,
            idempotency_key=request.idempotency_key,
            sanitized_arguments=arguments,
        )
        replay = _replay_claim(claim, operation)
        if replay is not None:
            return replay
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == request.conversation_id).with_for_update()
        )
        if conversation is None:
            raise RuntimeError("conversation disappeared while locked")
        if conversation.pending_action_type is not None:
            result = _booking_error_result(operation, BookingErrorCode.PENDING_ACTION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        booking = await session.scalar(
            select(Booking).where(Booking.id == request.booking_id).with_for_update()
        )
        if booking is None or booking.contact_id != conversation.contact_id:
            result = _booking_error_result(operation, BookingErrorCode.BOOKING_NOT_FOUND)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        if booking.status is not BookingStatus.CONFIRMED:
            result = _booking_error_result(operation, BookingErrorCode.BOOKING_STATE_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        if (
            booking.calendar_id is None
            or booking.calendar_event_id is None
            or booking.start_at is None
            or booking.end_at is None
        ):
            result = _booking_error_result(operation, BookingErrorCode.EXTERNAL_REFERENCE_MISSING)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        business_row = await session.get(BusinessConfig, 1)
        service = await session.get(Service, booking.service_id)
        if business_row is None or service is None:
            result = _booking_error_result(operation, BookingErrorCode.CONFIGURATION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        try:
            business = _business_spec(business_row)
            area_code = booking.booking_data.get("service_area_code")
            if not isinstance(area_code, str):
                raise ValueError("booking area is missing")
            area, area_error = _resolve_area(business, area_code)
            if area_error is not None or area is None:
                result = _booking_error_result(
                    operation, area_error or BookingErrorCode.UNSUPPORTED_SERVICE_AREA
                )
                _terminalize_execution(
                    session,
                    conversation=conversation,
                    execution=claim.execution,
                    result=result,
                    finished_at=now,
                )
                return result
            interval = normalize_requested_interval(
                request.requested_start_at,
                request.requested_end_at,
                service.default_duration_minutes,
            )
            policy = evaluate_booking_policy(business, interval, now)
        except ValueError:
            result = _booking_error_result(operation, BookingErrorCode.INVALID_REQUESTED_TIME)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        policy_error = _policy_error(policy)
        if policy_error is not None:
            result = _booking_error_result(operation, policy_error)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        configured_calendar_id = _calendar_id(calendar)
        if configured_calendar_id is None or configured_calendar_id != booking.calendar_id:
            result = _booking_error_result(operation, BookingErrorCode.EXTERNAL_STATE_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        availability = await check_calendar_availability(
            calendar,
            calendar_id=configured_calendar_id,
            policy=policy,
            exclude_event_id=booking.calendar_event_id,
        )
        availability_error = _availability_error(availability)
        if availability_error is not None:
            result = _booking_error_result(
                operation,
                availability_error,
                retryable=availability_error is BookingErrorCode.CALENDAR_UNAVAILABLE,
            )
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        action_token = generate_action_token()
        fingerprint = _expected_booking_fingerprint(booking)
        payload = _pending_payload(
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=action_token,
            fields={
                "booking_id": str(booking.id),
                "service_code": service.code,
                "service_area_code": area.code,
                "requested_start_at_utc": _iso_utc(interval.start_at_utc),
                "requested_end_at_utc": _iso_utc(interval.end_at_utc),
                "expected_state_fingerprint": fingerprint,
            },
        )
        _stage_pending_action(
            conversation,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            payload=payload,
            created_at=now,
        )
        result = PreparationResult(
            operation=operation,
            data=PreparationData(
                action_type=PendingActionType.RESCHEDULE_BOOKING,
                action_token=action_token,
                requested_start_at_utc=interval.start_at_utc,
                requested_end_at_utc=interval.end_at_utc,
            ),
        )
        _terminalize_execution(
            session,
            conversation=conversation,
            execution=claim.execution,
            result=result,
            finished_at=now,
        )
        return result


async def prepare_cancel_booking(
    session_factory: async_sessionmaker[AsyncSession],
    request: CancelBookingRequest,
) -> PreparationResult | BookingErrorResult:
    """Validate and stage a cancellation action without deleting a Calendar event."""
    operation: Literal["prepare_cancel_booking"] = "prepare_cancel_booking"
    result: PreparationResult | BookingErrorResult
    now = datetime.now(UTC)
    arguments = _cancel_arguments(request)
    async with session_factory.begin() as session:
        conversation = await session.get(Conversation, request.conversation_id)
        if conversation is None:
            return _booking_error_result(operation, BookingErrorCode.INVALID_INPUT)
        claim = await _claim_tool_execution(
            session,
            conversation_id=request.conversation_id,
            tool_name=operation,
            idempotency_key=request.idempotency_key,
            sanitized_arguments=arguments,
        )
        replay = _replay_claim(claim, operation)
        if replay is not None:
            return replay
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == request.conversation_id).with_for_update()
        )
        if conversation is None:
            raise RuntimeError("conversation disappeared while locked")
        if conversation.pending_action_type is not None:
            result = _booking_error_result(operation, BookingErrorCode.PENDING_ACTION_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        booking = await session.scalar(
            select(Booking).where(Booking.id == request.booking_id).with_for_update()
        )
        if booking is None or booking.contact_id != conversation.contact_id:
            result = _booking_error_result(operation, BookingErrorCode.BOOKING_NOT_FOUND)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        if booking.status is not BookingStatus.CONFIRMED:
            result = _booking_error_result(operation, BookingErrorCode.BOOKING_STATE_CONFLICT)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        if (
            booking.calendar_id is None
            or booking.calendar_event_id is None
            or booking.start_at is None
            or booking.end_at is None
        ):
            result = _booking_error_result(operation, BookingErrorCode.EXTERNAL_REFERENCE_MISSING)
            _terminalize_execution(
                session,
                conversation=conversation,
                execution=claim.execution,
                result=result,
                finished_at=now,
            )
            return result
        action_token = generate_action_token()
        payload = _pending_payload(
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=action_token,
            fields={
                "booking_id": str(booking.id),
                "expected_state_fingerprint": _expected_booking_fingerprint(booking),
                "cancellation_context": _safe_cancellation_context(request.cancellation_context),
            },
        )
        _stage_pending_action(
            conversation,
            action_type=PendingActionType.CANCEL_BOOKING,
            payload=payload,
            created_at=now,
        )
        result = PreparationResult(
            operation=operation,
            data=PreparationData(
                action_type=PendingActionType.CANCEL_BOOKING,
                action_token=action_token,
                requested_start_at_utc=booking.start_at,
                requested_end_at_utc=booking.end_at,
            ),
        )
        _terminalize_execution(
            session,
            conversation=conversation,
            execution=claim.execution,
            result=result,
            finished_at=now,
        )
        return result


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

"""Read-only validation of immutable Phase 2 booking operation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.application.booking import ConfirmationResult
from receptionist.db.models import Booking, Contact, Conversation, ToolExecution
from receptionist.domain.enums import BookingStatus, ToolExecutionStatus

type _AwareDateTime = Annotated[datetime, AwareDatetime]


class _BookingEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class BookingAction(StrEnum):
    CREATE_BOOKING = "create_booking"
    RESCHEDULE_BOOKING = "reschedule_booking"
    CANCEL_BOOKING = "cancel_booking"


class HistoricalBookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class BookingEvidenceValidationOutcome(StrEnum):
    VALID = "valid"
    MISSING_SOURCE_RECORD = "missing_source_record"
    INVALID_SOURCE_EXECUTION = "invalid_source_execution"
    HISTORICAL_RESULT_MISMATCH = "historical_result_mismatch"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    LOCAL_PERSISTENCE_FAILURE = "local_persistence_failure"


class BookingEvidenceValidationErrorCode(StrEnum):
    SOURCE_RECORD_MISSING = "source_record_missing"
    SOURCE_EXECUTION_INVALID = "source_execution_invalid"
    HISTORICAL_RESULT_MISMATCH = "historical_result_mismatch"
    CONTACT_OWNERSHIP_CONFLICT = "contact_ownership_conflict"
    LOCAL_PERSISTENCE_FAILURE = "local_persistence_failure"


class BookingEvidenceNextAction(StrEnum):
    NONE = "none"
    PROVIDE_VALID_SOURCE = "provide_valid_source"
    OPERATOR_REVIEW = "operator_review"
    RETRY = "retry"


class BookingEvidenceValidationRequest(_BookingEvidenceModel):
    """Only the local Booking and its immutable source execution may be supplied."""

    booking_id: UUID
    source_tool_execution_id: UUID


class BookingEvidence(_BookingEvidenceModel):
    """Small immutable facts derived from one successful historical operation."""

    booking_id: UUID
    source_tool_execution_id: UUID
    conversation_id: UUID
    contact_id: UUID
    action: BookingAction
    historical_status: HistoricalBookingStatus
    requested_start_at_utc: _AwareDateTime | None = None
    requested_end_at_utc: _AwareDateTime | None = None
    source_finished_at_utc: _AwareDateTime


class BookingEvidenceValidationResult(_BookingEvidenceModel):
    outcome: BookingEvidenceValidationOutcome
    evidence: BookingEvidence | None = None
    error_code: BookingEvidenceValidationErrorCode | None = None
    next_action: BookingEvidenceNextAction = BookingEvidenceNextAction.NONE


def _error_result(
    outcome: BookingEvidenceValidationOutcome,
    error_code: BookingEvidenceValidationErrorCode,
    next_action: BookingEvidenceNextAction,
) -> BookingEvidenceValidationResult:
    return BookingEvidenceValidationResult(
        outcome=outcome,
        error_code=error_code,
        next_action=next_action,
    )


def _normalize_aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


async def validate_booking_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    request: BookingEvidenceValidationRequest,
) -> BookingEvidenceValidationResult:
    """Validate source history and local ownership without mutating either record."""
    try:
        async with session_factory() as session:
            execution_row = (
                await session.execute(
                    select(
                        ToolExecution.id,
                        ToolExecution.conversation_id,
                        ToolExecution.tool_name,
                        ToolExecution.status,
                        ToolExecution.sanitized_arguments,
                        ToolExecution.sanitized_result,
                        ToolExecution.finished_at,
                    ).where(ToolExecution.id == request.source_tool_execution_id)
                )
            ).one_or_none()
            if execution_row is None:
                return _error_result(
                    BookingEvidenceValidationOutcome.MISSING_SOURCE_RECORD,
                    BookingEvidenceValidationErrorCode.SOURCE_RECORD_MISSING,
                    BookingEvidenceNextAction.PROVIDE_VALID_SOURCE,
                )
            (
                execution_id,
                conversation_id,
                tool_name,
                execution_status,
                sanitized_arguments,
                sanitized_result,
                finished_at,
            ) = execution_row

            if (
                tool_name != "confirm_booking_action"
                or execution_status is not ToolExecutionStatus.SUCCEEDED
            ):
                return _error_result(
                    BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION,
                    BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            finished_at_utc = _normalize_aware_utc(finished_at)
            if finished_at_utc is None:
                return _error_result(
                    BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION,
                    BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            raw_action = (
                sanitized_arguments.get("action_type")
                if isinstance(sanitized_arguments, Mapping)
                else None
            )
            if not isinstance(raw_action, str):
                return _error_result(
                    BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION,
                    BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )
            try:
                action = BookingAction(raw_action)
            except (TypeError, ValueError):
                return _error_result(
                    BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION,
                    BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            conversation_contact_id = await session.scalar(
                select(Conversation.contact_id).where(Conversation.id == conversation_id)
            )
            if conversation_contact_id is None:
                return _error_result(
                    BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT,
                    BookingEvidenceValidationErrorCode.CONTACT_OWNERSHIP_CONFLICT,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            booking_row = (
                await session.execute(
                    select(Booking.id, Booking.contact_id).where(Booking.id == request.booking_id)
                )
            ).one_or_none()
            if booking_row is None:
                return _error_result(
                    BookingEvidenceValidationOutcome.MISSING_SOURCE_RECORD,
                    BookingEvidenceValidationErrorCode.SOURCE_RECORD_MISSING,
                    BookingEvidenceNextAction.PROVIDE_VALID_SOURCE,
                )

            booking_id, booking_contact_id = booking_row
            contact_id = await session.scalar(
                select(Contact.id).where(Contact.id == booking_contact_id)
            )
            if (
                contact_id is None
                or conversation_contact_id != booking_contact_id
                or booking_contact_id != contact_id
            ):
                return _error_result(
                    BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT,
                    BookingEvidenceValidationErrorCode.CONTACT_OWNERSHIP_CONFLICT,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            if (
                not isinstance(sanitized_result, Mapping)
                or sanitized_result.get("ok") is not True
                or sanitized_result.get("operation") != "confirm_booking_action"
            ):
                return _error_result(
                    BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            try:
                historical_result = ConfirmationResult.model_validate(sanitized_result)
            except (TypeError, ValueError, ValidationError):
                return _error_result(
                    BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            if (
                historical_result.ok is not True
                or historical_result.operation != "confirm_booking_action"
                or historical_result.data.booking_id != request.booking_id
            ):
                return _error_result(
                    BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            expected_status = (
                BookingStatus.CANCELLED
                if action is BookingAction.CANCEL_BOOKING
                else BookingStatus.CONFIRMED
            )
            if historical_result.data.status is not expected_status:
                return _error_result(
                    BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            historical_start = _normalize_aware_utc(historical_result.data.requested_start_at_utc)
            historical_end = _normalize_aware_utc(historical_result.data.requested_end_at_utc)
            if (
                historical_result.data.requested_start_at_utc is not None
                and historical_start is None
            ) or (
                historical_result.data.requested_end_at_utc is not None and historical_end is None
            ):
                return _error_result(
                    BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH,
                    BookingEvidenceNextAction.OPERATOR_REVIEW,
                )

            evidence = BookingEvidence(
                booking_id=booking_id,
                source_tool_execution_id=execution_id,
                conversation_id=conversation_id,
                contact_id=contact_id,
                action=action,
                historical_status=HistoricalBookingStatus(historical_result.data.status.value),
                requested_start_at_utc=historical_start,
                requested_end_at_utc=historical_end,
                source_finished_at_utc=finished_at_utc,
            )
            return BookingEvidenceValidationResult(
                outcome=BookingEvidenceValidationOutcome.VALID,
                evidence=evidence,
            )
    except SQLAlchemyError:
        return _error_result(
            BookingEvidenceValidationOutcome.LOCAL_PERSISTENCE_FAILURE,
            BookingEvidenceValidationErrorCode.LOCAL_PERSISTENCE_FAILURE,
            BookingEvidenceNextAction.RETRY,
        )

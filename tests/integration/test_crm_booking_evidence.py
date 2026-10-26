from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from receptionist.application.booking import (
    BookingErrorCode,
    ConfirmationData,
    ConfirmationResult,
    _booking_error_result,
)
from receptionist.application.crm_booking_evidence import (
    BookingAction,
    BookingEvidenceNextAction,
    BookingEvidenceValidationErrorCode,
    BookingEvidenceValidationOutcome,
    BookingEvidenceValidationRequest,
    validate_booking_evidence,
)
from receptionist.db.models import (
    AuditEvent,
    Booking,
    Contact,
    Conversation,
    Service,
    ToolExecution,
)
from receptionist.domain.enums import (
    BookingStatus,
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
    ToolExecutionStatus,
)
from receptionist.seed import seed_reference_data

pytestmark = pytest.mark.integration

FINISHED_AT = datetime(2026, 10, 20, 10, 15, tzinfo=UTC)
START_AT = datetime(2026, 10, 21, 7, 30, tzinfo=UTC)
END_AT = datetime(2026, 10, 21, 8, 30, tzinfo=UTC)
CALENDAR_ID = "calendar-private-id"
CALENDAR_EVENT_ID = "calendar-private-event-id"
CUSTOMER_EMAIL = "private-customer@example.invalid"
CUSTOMER_NAME = "Private Customer Name"
TRANSCRIPT_MARKER = "Private transcript marker"
PROVIDER_PAYLOAD_MARKER = "Private provider payload marker"
_UNSET = object()


@dataclass(frozen=True)
class _SourceFixture:
    booking_id: uuid.UUID
    source_tool_execution_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    service_id: uuid.UUID


def _confirmation_result(
    booking_id: uuid.UUID,
    *,
    status: BookingStatus = BookingStatus.CONFIRMED,
    requested_start_at: datetime | None = START_AT,
    requested_end_at: datetime | None = END_AT,
) -> dict[str, Any]:
    """Serialize the existing Phase 2 result contract, including its optional fields."""
    return ConfirmationResult(
        data=ConfirmationData(
            booking_id=booking_id,
            status=status,
            requested_start_at_utc=requested_start_at,
            requested_end_at_utc=requested_end_at,
            calendar_id=CALENDAR_ID,
            calendar_event_id=CALENDAR_EVENT_ID,
        )
    ).model_dump(mode="json")


async def _create_source(
    session_factory,
    *,
    action: str = "create_booking",
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED,
    tool_name: str = "confirm_booking_action",
    finished_at: datetime | None = FINISHED_AT,
    historical_status: BookingStatus = BookingStatus.CONFIRMED,
    historical_start: datetime | None = START_AT,
    historical_end: datetime | None = END_AT,
    booking_data: dict[str, Any] | None = None,
    sanitized_arguments: dict[str, Any] | None = None,
    sanitized_result: dict[str, Any] | object | None = _UNSET,
    source_conversation_contact_id: uuid.UUID | None = None,
) -> _SourceFixture:
    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        service = await session.scalar(select(Service).where(Service.code == "plumbing"))
        assert service is not None
        contact = Contact(
            phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}",
            display_name=CUSTOMER_NAME,
            email=CUSTOMER_EMAIL,
        )
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            contact_id=source_conversation_contact_id or contact.id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
            recent_summary=TRANSCRIPT_MARKER,
        )
        session.add(conversation)
        await session.flush()
        booking = Booking(
            contact_id=contact.id,
            service_id=service.id,
            status=BookingStatus.CONFIRMED,
            start_at=START_AT,
            end_at=END_AT,
            calendar_id=CALENDAR_ID,
            calendar_event_id=f"{CALENDAR_EVENT_ID}-{uuid.uuid4().hex}",
            booking_data=booking_data
            or {
                "schema_version": 1,
                "last_operation": action,
                "requirements": {"private_note": PROVIDER_PAYLOAD_MARKER},
                "provider_payload": {"private": PROVIDER_PAYLOAD_MARKER},
            },
            confirmed_at=FINISHED_AT,
        )
        session.add(booking)
        await session.flush()
        execution = ToolExecution(
            conversation_id=conversation.id,
            tool_name=tool_name,
            status=status,
            sanitized_arguments=(
                sanitized_arguments
                if sanitized_arguments is not None
                else {
                    "conversation_id": str(conversation.id),
                    "action_type": action,
                    "action_token": str(uuid.uuid4()),
                }
            ),
            sanitized_result=(
                _confirmation_result(
                    booking.id,
                    status=historical_status,
                    requested_start_at=historical_start,
                    requested_end_at=historical_end,
                )
                if sanitized_result is _UNSET
                else sanitized_result
            ),
            finished_at=finished_at,
        )
        session.add(execution)
        await session.flush()
        return _SourceFixture(booking.id, execution.id, conversation.id, contact.id, service.id)


def _request(source: _SourceFixture) -> BookingEvidenceValidationRequest:
    return BookingEvidenceValidationRequest(
        booking_id=source.booking_id,
        source_tool_execution_id=source.source_tool_execution_id,
    )


async def _add_source_execution(
    session_factory,
    source: _SourceFixture,
    *,
    action: str,
    historical_status: BookingStatus,
    finished_at: datetime,
    historical_start: datetime | None,
    historical_end: datetime | None,
) -> _SourceFixture:
    async with session_factory.begin() as session:
        execution = ToolExecution(
            conversation_id=source.conversation_id,
            tool_name="confirm_booking_action",
            status=ToolExecutionStatus.SUCCEEDED,
            sanitized_arguments={
                "conversation_id": str(source.conversation_id),
                "action_type": action,
                "action_token": str(uuid.uuid4()),
            },
            sanitized_result=_confirmation_result(
                source.booking_id,
                status=historical_status,
                requested_start_at=historical_start,
                requested_end_at=historical_end,
            ),
            finished_at=finished_at,
        )
        session.add(execution)
        await session.flush()
        return _SourceFixture(
            source.booking_id,
            execution.id,
            source.conversation_id,
            source.contact_id,
            source.service_id,
        )


async def _validation(session_factory, source: _SourceFixture):
    return await validate_booking_evidence(session_factory, _request(source))


async def test_confirmed_create_returns_minimal_historical_evidence(session_factory) -> None:
    source = await _create_source(session_factory)

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.VALID
    assert result.error_code is None
    assert result.next_action is BookingEvidenceNextAction.NONE
    assert result.evidence is not None
    assert result.evidence.booking_id == source.booking_id
    assert result.evidence.source_tool_execution_id == source.source_tool_execution_id
    assert result.evidence.conversation_id == source.conversation_id
    assert result.evidence.contact_id == source.contact_id
    assert result.evidence.action is BookingAction.CREATE_BOOKING
    assert result.evidence.historical_status.value == "confirmed"
    assert result.evidence.requested_start_at_utc == START_AT
    assert result.evidence.requested_end_at_utc == END_AT
    assert result.evidence.source_finished_at_utc == FINISHED_AT


@pytest.mark.parametrize(
    ("action", "historical_status"),
    [
        ("reschedule_booking", BookingStatus.CONFIRMED),
        ("cancel_booking", BookingStatus.CANCELLED),
    ],
)
async def test_successful_reschedule_and_cancel_are_eligible(
    session_factory, action: str, historical_status: BookingStatus
) -> None:
    source = await _create_source(
        session_factory,
        action=action,
        historical_status=historical_status,
    )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.VALID
    assert result.evidence is not None
    assert result.evidence.action is BookingAction(action)
    assert result.evidence.historical_status.value == historical_status.value


async def test_missing_source_execution_returns_finite_safe_outcome(session_factory) -> None:
    request = BookingEvidenceValidationRequest(
        booking_id=uuid.uuid4(),
        source_tool_execution_id=uuid.uuid4(),
    )

    result = await validate_booking_evidence(session_factory, request)

    assert result.outcome is BookingEvidenceValidationOutcome.MISSING_SOURCE_RECORD
    assert result.error_code is BookingEvidenceValidationErrorCode.SOURCE_RECORD_MISSING
    assert result.next_action is BookingEvidenceNextAction.PROVIDE_VALID_SOURCE
    assert result.evidence is None


async def test_missing_booking_returns_finite_safe_outcome(session_factory) -> None:
    source = await _create_source(session_factory)
    missing_booking_id = uuid.uuid4()
    async with session_factory.begin() as session:
        execution = await session.get(ToolExecution, source.source_tool_execution_id)
        assert execution is not None and execution.sanitized_result is not None
        execution.sanitized_result = {
            **execution.sanitized_result,
            "data": {
                **execution.sanitized_result["data"],
                "booking_id": str(missing_booking_id),
            },
        }

    result = await validate_booking_evidence(
        session_factory,
        BookingEvidenceValidationRequest(
            booking_id=missing_booking_id,
            source_tool_execution_id=source.source_tool_execution_id,
        ),
    )

    assert result.outcome is BookingEvidenceValidationOutcome.MISSING_SOURCE_RECORD
    assert result.error_code is BookingEvidenceValidationErrorCode.SOURCE_RECORD_MISSING
    assert result.evidence is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool_name", "different_operation"),
        ("status", ToolExecutionStatus.STARTED),
        ("status", ToolExecutionStatus.FAILED),
        ("status", ToolExecutionStatus.REJECTED),
        ("finished_at", None),
    ],
)
async def test_invalid_source_execution_is_ineligible(
    session_factory, field: str, value: Any
) -> None:
    source = await _create_source(session_factory)
    async with session_factory.begin() as session:
        execution = await session.get(ToolExecution, source.source_tool_execution_id)
        assert execution is not None
        setattr(execution, field, value)

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION
    assert result.error_code is BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID
    assert result.next_action is BookingEvidenceNextAction.OPERATOR_REVIEW
    assert result.evidence is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"action_type": "delete_booking"},
        {"action_type": None},
        {"action_type": 7},
    ],
)
async def test_missing_or_unsupported_historical_action_is_ineligible(
    session_factory, arguments: dict[str, Any]
) -> None:
    source = await _create_source(session_factory, sanitized_arguments=arguments)

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION
    assert result.error_code is BookingEvidenceValidationErrorCode.SOURCE_EXECUTION_INVALID
    assert result.evidence is None


@pytest.mark.parametrize(
    "stored_result",
    [
        None,
        {"unrecognized": "result"},
        _booking_error_result("confirm_booking_action", BookingErrorCode.INVALID_INPUT).model_dump(
            mode="json"
        ),
        {
            "ok": True,
            "operation": "different_operation",
            "data": {"booking_id": str(uuid.uuid4()), "status": "confirmed"},
        },
    ],
)
async def test_missing_malformed_unsuccessful_or_wrong_operation_result_is_ineligible(
    session_factory, stored_result: dict[str, Any] | None
) -> None:
    source = await _create_source(session_factory, sanitized_result=stored_result)

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH
    assert result.error_code is BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH
    assert result.next_action is BookingEvidenceNextAction.OPERATOR_REVIEW
    assert result.evidence is None


async def test_historical_result_for_another_booking_is_rejected(session_factory) -> None:
    source = await _create_source(
        session_factory,
        sanitized_result=_confirmation_result(uuid.uuid4()),
    )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH
    assert result.error_code is BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH
    assert result.evidence is None


@pytest.mark.parametrize("variant", ["missing_ok", "missing_operation", "numeric_ok"])
async def test_historical_result_requires_explicit_success_markers(
    session_factory, variant: str
) -> None:
    source = await _create_source(session_factory)
    async with session_factory.begin() as session:
        if variant == "numeric_ok":
            await session.execute(
                text(
                    "UPDATE tool_execution SET sanitized_result = "
                    "jsonb_set(sanitized_result, '{ok}', '1'::jsonb) WHERE id = :execution_id"
                ),
                {"execution_id": source.source_tool_execution_id},
            )
        else:
            execution = await session.get(ToolExecution, source.source_tool_execution_id)
            assert execution is not None and execution.sanitized_result is not None
            stored_result = dict(execution.sanitized_result)
            stored_result.pop("ok" if variant == "missing_ok" else "operation")
            execution.sanitized_result = stored_result

    if variant == "numeric_ok":
        async with session_factory() as session:
            execution = await session.get(ToolExecution, source.source_tool_execution_id)
            assert execution is not None and execution.sanitized_result is not None
            assert type(execution.sanitized_result["ok"]) is int

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH
    assert result.error_code is BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH
    assert result.evidence is None


@pytest.mark.parametrize(
    ("action", "historical_status"),
    [
        ("create_booking", BookingStatus.CANCELLED),
        ("reschedule_booking", BookingStatus.CANCELLED),
        ("cancel_booking", BookingStatus.CONFIRMED),
    ],
)
async def test_historical_result_status_must_match_source_action(
    session_factory, action: str, historical_status: BookingStatus
) -> None:
    source = await _create_source(
        session_factory,
        action=action,
        historical_status=historical_status,
    )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.HISTORICAL_RESULT_MISMATCH
    assert result.error_code is BookingEvidenceValidationErrorCode.HISTORICAL_RESULT_MISMATCH
    assert result.evidence is None


async def test_source_conversation_must_belong_to_booking_contact(session_factory) -> None:
    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        service = await session.scalar(select(Service).where(Service.code == "plumbing"))
        assert service is not None
        booking_contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
        conversation_contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
        session.add_all([booking_contact, conversation_contact])
        await session.flush()
        conversation = Conversation(
            contact_id=conversation_contact.id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(conversation)
        await session.flush()
        booking = Booking(
            contact_id=booking_contact.id,
            service_id=service.id,
            status=BookingStatus.CONFIRMED,
            start_at=START_AT,
            end_at=END_AT,
            calendar_id=CALENDAR_ID,
            calendar_event_id=f"{CALENDAR_EVENT_ID}-{uuid.uuid4().hex}",
            booking_data={"last_operation": "create_booking"},
            confirmed_at=FINISHED_AT,
        )
        session.add(booking)
        await session.flush()
        execution = ToolExecution(
            conversation_id=conversation.id,
            tool_name="confirm_booking_action",
            status=ToolExecutionStatus.SUCCEEDED,
            sanitized_arguments={"action_type": "create_booking"},
            sanitized_result=_confirmation_result(booking.id),
            finished_at=FINISHED_AT,
        )
        session.add(execution)
        await session.flush()
        source = _SourceFixture(
            booking.id, execution.id, conversation.id, booking_contact.id, service.id
        )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT
    assert result.error_code is BookingEvidenceValidationErrorCode.CONTACT_OWNERSHIP_CONFLICT
    assert result.next_action is BookingEvidenceNextAction.OPERATOR_REVIEW
    assert result.evidence is None


async def _create_source_with_missing_conversation(session_factory) -> _SourceFixture:
    source = await _create_source(session_factory)
    orphan_conversation_id = uuid.uuid4()
    async with session_factory.begin() as session:
        await session.execute(text("ALTER TABLE tool_execution DISABLE TRIGGER ALL"))
        execution = ToolExecution(
            conversation_id=orphan_conversation_id,
            tool_name="confirm_booking_action",
            status=ToolExecutionStatus.SUCCEEDED,
            sanitized_arguments={"action_type": "create_booking"},
            sanitized_result=_confirmation_result(source.booking_id),
            finished_at=FINISHED_AT,
        )
        session.add(execution)
        await session.flush()
        await session.execute(text("ALTER TABLE tool_execution ENABLE TRIGGER ALL"))
        source = _SourceFixture(
            source.booking_id,
            execution.id,
            orphan_conversation_id,
            source.contact_id,
            source.service_id,
        )
    return source


async def test_missing_source_conversation_is_an_ownership_conflict(session_factory) -> None:
    source = await _create_source_with_missing_conversation(session_factory)
    try:
        result = await _validation(session_factory, source)
    finally:
        async with session_factory.begin() as session:
            execution = await session.get(ToolExecution, source.source_tool_execution_id)
            if execution is not None:
                await session.delete(execution)

    assert result.outcome is BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT
    assert result.error_code is BookingEvidenceValidationErrorCode.CONTACT_OWNERSHIP_CONFLICT
    assert result.evidence is None


async def test_missing_booking_contact_is_an_ownership_conflict(session_factory) -> None:
    source = await _create_source(session_factory)
    missing_contact_id = uuid.uuid4()
    orphan_conversation_id = uuid.uuid4()
    async with session_factory.begin() as session:
        await session.execute(text("ALTER TABLE conversation DISABLE TRIGGER ALL"))
        await session.execute(text("ALTER TABLE booking DISABLE TRIGGER ALL"))
        await session.execute(text("ALTER TABLE tool_execution DISABLE TRIGGER ALL"))
        conversation = Conversation(
            id=orphan_conversation_id,
            contact_id=missing_contact_id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        booking = Booking(
            contact_id=missing_contact_id,
            service_id=source.service_id,
            status=BookingStatus.PENDING,
            start_at=None,
            end_at=None,
            calendar_id=None,
            calendar_event_id=None,
            booking_data={"last_operation": "create_booking"},
        )
        session.add_all([conversation, booking])
        await session.flush()
        execution = await session.get(ToolExecution, source.source_tool_execution_id)
        assert execution is not None
        execution.conversation_id = orphan_conversation_id
        execution.sanitized_result = _confirmation_result(booking.id)
        await session.flush()
        await session.execute(text("ALTER TABLE tool_execution ENABLE TRIGGER ALL"))
        await session.execute(text("ALTER TABLE booking ENABLE TRIGGER ALL"))
        await session.execute(text("ALTER TABLE conversation ENABLE TRIGGER ALL"))
        missing_source = _SourceFixture(
            booking.id,
            source.source_tool_execution_id,
            orphan_conversation_id,
            missing_contact_id,
            source.service_id,
        )
    try:
        result = await _validation(session_factory, missing_source)
    finally:
        async with session_factory.begin() as session:
            execution = await session.get(ToolExecution, source.source_tool_execution_id)
            if execution is not None:
                execution.conversation_id = source.conversation_id
                execution.sanitized_result = _confirmation_result(source.booking_id)
            booking = await session.get(Booking, missing_source.booking_id)
            if booking is not None:
                await session.delete(booking)
            conversation = await session.get(Conversation, orphan_conversation_id)
            if conversation is not None:
                await session.delete(conversation)

    assert result.outcome is BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT
    assert result.error_code is BookingEvidenceValidationErrorCode.CONTACT_OWNERSHIP_CONFLICT
    assert result.evidence is None


async def test_timestamp_and_historical_slot_are_normalized_to_utc(session_factory) -> None:
    utc_plus_three = timezone(timedelta(hours=3))
    source_finished = datetime(2026, 10, 20, 13, 15, tzinfo=utc_plus_three)
    source_start = datetime(2026, 10, 21, 10, 30, tzinfo=utc_plus_three)
    source_end = datetime(2026, 10, 21, 11, 30, tzinfo=utc_plus_three)
    source = await _create_source(
        session_factory,
        finished_at=source_finished,
        historical_start=source_start,
        historical_end=source_end,
    )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.VALID
    assert result.evidence is not None
    assert result.evidence.source_finished_at_utc == datetime(2026, 10, 20, 10, 15, tzinfo=UTC)
    assert result.evidence.requested_start_at_utc == datetime(2026, 10, 21, 7, 30, tzinfo=UTC)
    assert result.evidence.requested_end_at_utc == datetime(2026, 10, 21, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("historical_start", "historical_end"),
    [(None, None), (START_AT, None), (None, END_AT)],
)
async def test_optional_historical_slot_fields_follow_phase_two_contract(
    session_factory,
    historical_start: datetime | None,
    historical_end: datetime | None,
) -> None:
    source = await _create_source(
        session_factory,
        historical_start=historical_start,
        historical_end=historical_end,
    )

    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.VALID
    assert result.evidence is not None
    assert result.evidence.requested_start_at_utc == historical_start
    assert result.evidence.requested_end_at_utc == historical_end


async def test_prior_booking_operations_remain_eligible_after_mutable_changes(
    session_factory,
) -> None:
    initial = await _create_source(session_factory)
    rescheduled_start = START_AT + timedelta(hours=1)
    rescheduled_end = END_AT + timedelta(hours=1)
    reschedule = await _add_source_execution(
        session_factory,
        initial,
        action="reschedule_booking",
        historical_status=BookingStatus.CONFIRMED,
        finished_at=FINISHED_AT + timedelta(minutes=10),
        historical_start=rescheduled_start,
        historical_end=rescheduled_end,
    )
    cancelled = await _add_source_execution(
        session_factory,
        initial,
        action="cancel_booking",
        historical_status=BookingStatus.CANCELLED,
        finished_at=FINISHED_AT + timedelta(minutes=20),
        historical_start=rescheduled_start,
        historical_end=rescheduled_end,
    )

    # Use the same mutable Booking and Conversation for the three stored operations.
    async with session_factory.begin() as session:
        initial_booking = await session.get(Booking, initial.booking_id)
        assert initial_booking is not None
        initial_booking.status = BookingStatus.CANCELLED
        initial_booking.start_at = START_AT + timedelta(hours=3)
        initial_booking.end_at = END_AT + timedelta(hours=3)
        initial_booking.booking_data = {"last_operation": "cancel_booking"}

    for source, expected_action, expected_status, expected_start in (
        (initial, BookingAction.CREATE_BOOKING, "confirmed", START_AT),
        (reschedule, BookingAction.RESCHEDULE_BOOKING, "confirmed", rescheduled_start),
        (cancelled, BookingAction.CANCEL_BOOKING, "cancelled", rescheduled_start),
    ):
        result = await validate_booking_evidence(
            session_factory,
            BookingEvidenceValidationRequest(
                booking_id=initial.booking_id,
                source_tool_execution_id=source.source_tool_execution_id,
            ),
        )
        assert result.outcome is BookingEvidenceValidationOutcome.VALID
        assert result.evidence is not None
        assert result.evidence.action is expected_action
        assert result.evidence.historical_status.value == expected_status
        assert result.evidence.requested_start_at_utc == expected_start


async def test_safe_result_excludes_contact_data_transcript_and_provider_payload(
    session_factory,
) -> None:
    source = await _create_source(session_factory)

    result = await _validation(session_factory, source)
    safe_output = result.model_dump_json()

    for private_value in (
        CUSTOMER_EMAIL,
        CUSTOMER_NAME,
        TRANSCRIPT_MARKER,
        PROVIDER_PAYLOAD_MARKER,
        CALENDAR_ID,
        CALENDAR_EVENT_ID,
    ):
        assert private_value not in safe_output


async def test_validation_does_not_mutate_booking_execution_or_audit(session_factory) -> None:
    source = await _create_source(session_factory)
    async with session_factory() as session:
        execution_before = await session.get(ToolExecution, source.source_tool_execution_id)
        booking_before = await session.get(Booking, source.booking_id)
        audit_count_before = await session.scalar(select(func.count()).select_from(AuditEvent))
        assert execution_before is not None and booking_before is not None
        prior_execution = (
            execution_before.status,
            execution_before.sanitized_arguments,
            execution_before.sanitized_result,
            execution_before.finished_at,
        )
        prior_booking = (
            booking_before.status,
            booking_before.start_at,
            booking_before.end_at,
            booking_before.booking_data,
        )

    await _validation(session_factory, source)

    async with session_factory() as session:
        execution_after = await session.get(ToolExecution, source.source_tool_execution_id)
        booking_after = await session.get(Booking, source.booking_id)
        audit_count_after = await session.scalar(select(func.count()).select_from(AuditEvent))
        assert execution_after is not None and booking_after is not None
        assert (
            execution_after.status,
            execution_after.sanitized_arguments,
            execution_after.sanitized_result,
            execution_after.finished_at,
        ) == prior_execution
        assert (
            booking_after.status,
            booking_after.start_at,
            booking_after.end_at,
            booking_after.booking_data,
        ) == prior_booking
        assert audit_count_after == audit_count_before


async def test_validation_makes_no_hubspot_http_request(monkeypatch, session_factory) -> None:
    source = await _create_source(session_factory)

    def reject_provider_request(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("booking evidence validation must not call HubSpot")

    monkeypatch.setattr(httpx.AsyncClient, "request", reject_provider_request)
    result = await _validation(session_factory, source)

    assert result.outcome is BookingEvidenceValidationOutcome.VALID


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "action_type",
        "status",
        "timestamp",
        "crm_contact_id",
        "hubspot_contact_id",
        "note_body",
        "provider_properties",
    ],
)
def test_request_rejects_caller_supplied_historical_or_crm_fields(
    forbidden_field: str,
) -> None:
    with pytest.raises(ValidationError):
        BookingEvidenceValidationRequest(
            booking_id=uuid.uuid4(),
            source_tool_execution_id=uuid.uuid4(),
            **{forbidden_field: "caller-controlled"},
        )


async def test_database_failure_returns_safe_retryable_outcome() -> None:
    class _BrokenSessionFactory:
        def __call__(self):
            raise SQLAlchemyError("private database diagnostics")

    result = await validate_booking_evidence(
        _BrokenSessionFactory(),
        BookingEvidenceValidationRequest(
            booking_id=uuid.uuid4(),
            source_tool_execution_id=uuid.uuid4(),
        ),
    )

    assert result.outcome is BookingEvidenceValidationOutcome.LOCAL_PERSISTENCE_FAILURE
    assert result.error_code is BookingEvidenceValidationErrorCode.LOCAL_PERSISTENCE_FAILURE
    assert result.next_action is BookingEvidenceNextAction.RETRY
    assert result.evidence is None
    assert "private database diagnostics" not in result.model_dump_json()

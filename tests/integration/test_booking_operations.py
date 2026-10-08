from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from receptionist.application.booking import (
    BookingErrorCode,
    BookingErrorResult,
    CancelBookingRequest,
    ConfirmationResult,
    ConfirmBookingActionRequest,
    CreateBookingRequest,
    PreparationResult,
    RescheduleBookingRequest,
    ToolExecutionClaimOutcome,
    _claim_tool_execution,
    _deterministic_calendar_event_id,
    confirm_booking_action,
    prepare_cancel_booking,
    prepare_create_booking,
    prepare_reschedule_booking,
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
    PendingActionStatus,
    PendingActionType,
    ToolExecutionStatus,
)
from receptionist.integrations.google_calendar import (
    CalendarClientError,
    CalendarErrorCode,
    CalendarEventCreate,
    CalendarInterval,
)
from receptionist.seed import seed_reference_data
from tests.support.calendar_double import DeterministicCalendarDouble

pytestmark = pytest.mark.integration

CALENDAR_ID = "calendar-id"
NOW_UTC = datetime(2026, 10, 11, 3, tzinfo=UTC)
REQUESTED_START = datetime(2026, 10, 11, 5, 30, tzinfo=UTC)
REQUESTED_END = datetime(2026, 10, 11, 6, 30, tzinfo=UTC)


async def _conversation_ids(session_factory, count: int = 1) -> tuple[uuid.UUID, ...]:
    async with session_factory.begin() as session:
        ids: list[uuid.UUID] = []
        for _ in range(count):
            contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
            session.add(contact)
            await session.flush()
            conversation = Conversation(
                contact_id=contact.id,
                channel=ConversationChannel.PHONE,
                status=ConversationStatus.OPEN,
                control_mode=ControlMode.AI,
                language_mode=ConversationLanguageMode.UNKNOWN,
            )
            session.add(conversation)
            await session.flush()
            ids.append(conversation.id)
        return tuple(ids)


async def _operation_context(session_factory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            contact_id=contact.id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(conversation)
        await session.flush()
        service_id = await session.scalar(select(Service.id).where(Service.code == "plumbing"))
        assert service_id is not None
        service = await session.get(Service, service_id)
        assert service is not None
        service.active = True
        service.bookable = True
        service.booking_requirements = []
        return contact.id, conversation.id, service_id


def _calendar_double(**kwargs) -> DeterministicCalendarDouble:
    return DeterministicCalendarDouble(calendar_id=CALENDAR_ID, **kwargs)


def _create_request(conversation_id: uuid.UUID, key: str) -> CreateBookingRequest:
    return CreateBookingRequest(
        conversation_id=conversation_id,
        service_selector="plumbing",
        service_area_selector="al_olaya",
        requested_start_at=REQUESTED_START,
        requested_end_at=REQUESTED_END,
        idempotency_key=key,
    )


def _booking_data(
    *, service_code: str = "plumbing", area_code: str = "al_olaya"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "service_code": service_code,
        "service_area_code": area_code,
        "requested_start_at_utc": REQUESTED_START.isoformat().replace("+00:00", "Z"),
        "requested_end_at_utc": REQUESTED_END.isoformat().replace("+00:00", "Z"),
        "requirements": {},
        "last_operation": "create_booking",
    }


async def _confirmed_booking(
    session_factory,
    *,
    contact_id: uuid.UUID,
    service_id: uuid.UUID,
    start_at: datetime = REQUESTED_START,
    event_id: str | None = None,
) -> uuid.UUID:
    event_id = event_id or f"target-event-{uuid.uuid4().hex}"
    async with session_factory.begin() as session:
        booking = Booking(
            contact_id=contact_id,
            service_id=service_id,
            status=BookingStatus.CONFIRMED,
            start_at=start_at,
            end_at=start_at + (REQUESTED_END - REQUESTED_START),
            calendar_id=CALENDAR_ID,
            calendar_event_id=event_id,
            booking_data=_booking_data(),
            confirmed_at=NOW_UTC,
        )
        session.add(booking)
        await session.flush()
        return booking.id


def _finish_success(claim) -> None:
    claim.execution.status = ToolExecutionStatus.SUCCEEDED
    claim.execution.sanitized_result = {"ok": True, "operation": claim.execution.tool_name}
    claim.execution.finished_at = datetime.now(UTC)


async def test_new_tool_execution_claim_creates_one_row(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"claim-{uuid.uuid4()}"

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments={"service_code": "plumbing"},
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW
        assert claim.execution.status is ToolExecutionStatus.STARTED
        _finish_success(claim)

    async with session_factory() as session:
        rows = (
            await session.scalars(select(ToolExecution).where(ToolExecution.idempotency_key == key))
        ).all()
        assert len(rows) == 1
        assert rows[0].status is ToolExecutionStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status,outcome",
    [
        (ToolExecutionStatus.SUCCEEDED, ToolExecutionClaimOutcome.SUCCEEDED_REPLAY),
        (ToolExecutionStatus.REJECTED, ToolExecutionClaimOutcome.REJECTED_REPLAY),
        (ToolExecutionStatus.FAILED, ToolExecutionClaimOutcome.FAILED_REPLAY),
    ],
)
async def test_terminal_tool_execution_claims_replay_stored_result(
    session_factory, status: ToolExecutionStatus, outcome: ToolExecutionClaimOutcome
) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"terminal-{uuid.uuid4()}"

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=key,
            sanitized_arguments={"booking_id": "booking-id"},
        )
        claim.execution.status = status
        claim.execution.sanitized_result = {"ok": status is ToolExecutionStatus.SUCCEEDED}
        claim.execution.safe_error_code = (
            "booking_state_conflict" if status is not ToolExecutionStatus.SUCCEEDED else None
        )
        claim.execution.finished_at = datetime.now(UTC)

    async with session_factory.begin() as session:
        replay = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=key,
            sanitized_arguments={"booking_id": "booking-id"},
        )
        assert replay.outcome is outcome
        assert replay.execution.sanitized_result == {"ok": status is ToolExecutionStatus.SUCCEEDED}


@pytest.mark.parametrize(
    "status,outcome",
    [
        (ToolExecutionStatus.SUCCEEDED, ToolExecutionClaimOutcome.SUCCEEDED_REPLAY),
        (ToolExecutionStatus.REJECTED, ToolExecutionClaimOutcome.REJECTED_REPLAY),
        (ToolExecutionStatus.FAILED, ToolExecutionClaimOutcome.FAILED_REPLAY),
    ],
)
async def test_exact_terminal_key_replays_before_unrelated_started_execution(
    session_factory, status: ToolExecutionStatus, outcome: ToolExecutionClaimOutcome
) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    terminal_key = f"terminal-priority-{uuid.uuid4()}"
    active_key = f"active-unrelated-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id"}

    async with session_factory.begin() as session:
        terminal = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=terminal_key,
            sanitized_arguments=arguments,
        )
        terminal.execution.status = status
        terminal.execution.sanitized_result = {"status": status.value}
        terminal.execution.safe_error_code = (
            "booking_state_conflict" if status is not ToolExecutionStatus.SUCCEEDED else None
        )
        terminal.execution.finished_at = datetime.now(UTC)

    async with session_factory.begin() as session:
        active = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=active_key,
            sanitized_arguments=arguments,
        )
        assert active.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        replay = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=terminal_key,
            sanitized_arguments=arguments,
        )

    assert replay.outcome is outcome
    assert replay.execution.sanitized_result == {"status": status.value}


async def test_exact_started_key_recovers_before_unrelated_started_execution(
    session_factory,
) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    requested_key = f"started-priority-{uuid.uuid4()}"
    unrelated_key = f"started-unrelated-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id"}

    async with session_factory.begin() as session:
        requested = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=requested_key,
            sanitized_arguments=arguments,
        )
        assert requested.outcome is ToolExecutionClaimOutcome.NEW
        session.add(
            ToolExecution(
                conversation_id=conversation_id,
                tool_name="confirm_booking_action",
                status=ToolExecutionStatus.STARTED,
                idempotency_key=unrelated_key,
                sanitized_arguments=arguments,
            )
        )

    async with session_factory.begin() as session:
        recovery = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=requested_key,
            sanitized_arguments=arguments,
        )

    assert recovery.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY
    assert recovery.execution.idempotency_key == requested_key


async def test_exact_key_mismatch_is_not_masked_by_unrelated_started_execution(
    session_factory,
) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"mismatch-priority-{uuid.uuid4()}"
    unrelated_key = f"active-unrelated-{uuid.uuid4()}"

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments={"service_code": "plumbing"},
        )
        _finish_success(claim)

    async with session_factory.begin() as session:
        active = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=unrelated_key,
            sanitized_arguments={"service_code": "plumbing"},
        )
        assert active.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        conflict = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments={"service_code": "electrical"},
        )

    assert conflict.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT


async def test_started_tool_execution_claim_is_recoverable_by_same_key(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"started-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id", "expected_state_fingerprint": "fingerprint"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        recovery = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        assert recovery.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY
        assert recovery.execution.status is ToolExecutionStatus.STARTED


@pytest.mark.parametrize("mismatch", ["tool", "conversation", "arguments"])
async def test_reusing_key_for_different_logical_identity_is_conflict(
    session_factory, mismatch: str
) -> None:
    conversation_id, other_conversation_id = await _conversation_ids(session_factory, count=2)
    key = f"mismatch-{uuid.uuid4()}"
    arguments = {"service_code": "plumbing"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        _finish_success(claim)

    async with session_factory.begin() as session:
        conflict = await _claim_tool_execution(
            session,
            conversation_id=(
                other_conversation_id if mismatch == "conversation" else conversation_id
            ),
            tool_name=(
                "prepare_cancel_booking" if mismatch == "tool" else "prepare_create_booking"
            ),
            idempotency_key=key,
            sanitized_arguments=(
                {"service_code": "electrical"} if mismatch == "arguments" else arguments
            ),
        )
        assert conflict.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT
        assert await session.scalar(select(func.count()).select_from(ToolExecution)) >= 1


async def test_started_execution_blocks_a_different_key_for_same_operation(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    first_key = f"active-{uuid.uuid4()}"
    second_key = f"active-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=first_key,
            sanitized_arguments=arguments,
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        blocked = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=second_key,
            sanitized_arguments=arguments,
        )
        assert blocked.outcome is ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS
        assert await session.scalar(select(func.count()).select_from(ToolExecution)) >= 1


async def test_same_key_contention_creates_one_row_and_loser_replays(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"contention-{uuid.uuid4()}"
    barrier = asyncio.Barrier(2)

    async def contender():
        async with session_factory.begin() as session:
            await barrier.wait()
            claim = await _claim_tool_execution(
                session,
                conversation_id=conversation_id,
                tool_name="prepare_create_booking",
                idempotency_key=key,
                sanitized_arguments={"service_code": "plumbing"},
            )
            if claim.outcome is ToolExecutionClaimOutcome.NEW:
                _finish_success(claim)
            return claim.outcome

    outcomes = await asyncio.gather(contender(), contender())
    assert sorted(outcome.value for outcome in outcomes) == ["new", "succeeded_replay"]

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(ToolExecution.idempotency_key == key)
            )
            == 1
        )


async def test_prepare_create_stages_one_sanitized_action_without_provider_mutation(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    key = f"prepare-create-{uuid.uuid4()}"

    result = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, key),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, PreparationResult)
    assert result.data.action_type.value == "create_booking"
    assert result.data.confirmation_required is True
    assert result.data.requested_start_at_utc == REQUESTED_START
    assert len(calendar.free_busy_queries) == 1
    assert calendar.mutation_calls == []

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.pending_action_payload is not None
        assert conversation.pending_action_payload["action_token"] == result.data.action_token
        assert conversation.pending_action_type.value == "create_booking"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Booking)
                .where(Booking.contact_id == conversation.contact_id)
            )
            == 0
        )
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.conversation_id == conversation_id)
        )
        assert execution is not None
        assert execution.status is ToolExecutionStatus.SUCCEEDED
        assert audit is not None
        assert "phone" not in str(execution.sanitized_arguments).casefold()
        assert "provider" not in str(execution.sanitized_arguments).casefold()


async def test_prepare_create_same_key_replays_and_different_key_preserves_action(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    first_key = f"prepare-replay-{uuid.uuid4()}"

    first = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, first_key),
        now_utc=NOW_UTC,
    )
    replay = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, first_key),
        now_utc=NOW_UTC,
    )
    conflict = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-other-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )

    assert isinstance(first, PreparationResult)
    assert isinstance(replay, PreparationResult)
    assert replay.replayed is True
    assert replay.data.action_token == first.data.action_token
    assert isinstance(conflict, BookingErrorResult)
    assert conflict.error.code is BookingErrorCode.PENDING_ACTION_CONFLICT
    assert len(calendar.free_busy_queries) == 1


@pytest.mark.parametrize(
    "request_update,expected_code",
    [
        ({"service_selector": "missing-service"}, BookingErrorCode.UNKNOWN_SERVICE),
        ({"service_area_selector": "missing-area"}, BookingErrorCode.UNSUPPORTED_SERVICE_AREA),
        (
            {"requested_start_at": datetime(2026, 10, 11, 4, 30, tzinfo=UTC)},
            BookingErrorCode.OUTSIDE_BUSINESS_POLICY,
        ),
    ],
)
async def test_prepare_create_rejects_invalid_requests_without_pending_action(
    session_factory, request_update: dict[str, object], expected_code: BookingErrorCode
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    request_data = _create_request(conversation_id, f"prepare-reject-{uuid.uuid4()}").model_dump()
    request_data.update(request_update)
    request = CreateBookingRequest.model_validate(request_data)

    result = await prepare_create_booking(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is expected_code
    assert calendar.free_busy_queries == []

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.pending_action_type is None


async def test_prepare_create_rejects_calendar_unavailable_and_persists_rejection(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(
        free_busy_error=CalendarClientError(
            CalendarErrorCode.CALENDAR_UNAVAILABLE,
            "provider body must not escape",
        )
    )
    key = f"prepare-unavailable-{uuid.uuid4()}"

    result = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, key),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    async with session_factory() as session:
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )
        assert execution is not None
        assert execution.status is ToolExecutionStatus.REJECTED


@pytest.mark.parametrize(
    "field,expected_code",
    [
        ("active", BookingErrorCode.SERVICE_INACTIVE),
        ("bookable", BookingErrorCode.SERVICE_NOT_BOOKABLE),
    ],
)
async def test_prepare_create_distinguishes_inactive_and_non_bookable_services(
    session_factory, field: str, expected_code: BookingErrorCode
) -> None:
    _, conversation_id, service_id = await _operation_context(session_factory)
    async with session_factory.begin() as session:
        service = await session.get(Service, service_id)
        assert service is not None
        setattr(service, field, False)

    result = await prepare_create_booking(
        session_factory,
        _calendar_double(),
        _create_request(conversation_id, f"prepare-service-state-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is expected_code


@pytest.mark.parametrize(
    "requirements",
    [{"unexpected": "value"}, {}],
)
async def test_prepare_create_rejects_unknown_or_missing_configured_requirements(
    session_factory, requirements: dict[str, str]
) -> None:
    _, conversation_id, service_id = await _operation_context(session_factory)
    async with session_factory.begin() as session:
        service = await session.get(Service, service_id)
        assert service is not None
        service.booking_requirements = [
            {
                "key": "property_type",
                "label_en": "Property type",
                "label_ar": "نوع العقار",
                "required": True,
            }
        ]
    request = _create_request(conversation_id, f"prepare-requirements-{uuid.uuid4()}").model_copy(
        update={"requirements": requirements}
    )

    result = await prepare_create_booking(
        session_factory, _calendar_double(), request, now_utc=NOW_UTC
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.INVALID_INPUT


async def test_prepare_reschedule_enforces_conversation_contact_ownership(session_factory) -> None:
    contact_id, _, service_id = await _operation_context(session_factory)
    _, other_conversation_id, _ = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )

    result = await prepare_reschedule_booking(
        session_factory,
        _calendar_double(),
        RescheduleBookingRequest(
            conversation_id=other_conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START,
            idempotency_key=f"prepare-owner-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.BOOKING_NOT_FOUND


async def test_prepare_reschedule_stages_fingerprint_and_reads_only_calendar_conflicts(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        target_event_id = booking.calendar_event_id
        assert target_event_id is not None
    calendar = _calendar_double()
    await calendar.create_event(
        CALENDAR_ID,
        target_event_id,
        CalendarEventCreate(
            interval=CalendarInterval(REQUESTED_START, REQUESTED_END),
            private_booking_id=str(booking_id),
        ),
    )
    calendar.mutation_calls.clear()
    request = RescheduleBookingRequest(
        conversation_id=conversation_id,
        booking_id=booking_id,
        requested_start_at=REQUESTED_START + (REQUESTED_END - REQUESTED_START),
        idempotency_key=f"prepare-reschedule-{uuid.uuid4()}",
    )

    result = await prepare_reschedule_booking(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, PreparationResult)
    assert result.data.action_type.value == "reschedule_booking"
    assert calendar.conflict_queries == [
        (
            CALENDAR_ID,
            REQUESTED_START + timedelta(minutes=45),
            REQUESTED_END + timedelta(hours=1, minutes=15),
            target_event_id,
        )
    ]
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == REQUESTED_START
        assert conversation.pending_action_payload is not None
        assert len(conversation.pending_action_payload["expected_state_fingerprint"]) == 64


async def test_prepare_cancel_requires_owned_managed_booking_and_only_stages_action(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    cancellation_context = "customer requested cancellation"
    key = f"prepare-cancel-{uuid.uuid4()}"
    request = CancelBookingRequest(
        conversation_id=conversation_id,
        booking_id=booking_id,
        cancellation_context=cancellation_context,
        idempotency_key=key,
    )

    result = await prepare_cancel_booking(session_factory, request)
    replay = await prepare_cancel_booking(session_factory, request)

    assert isinstance(result, PreparationResult)
    assert isinstance(replay, PreparationResult)
    assert replay.replayed is True
    assert replay.data.action_token == result.data.action_token
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )
        audit_events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.conversation_id == conversation_id)
            )
        ).all()
        assert booking is not None
        assert conversation is not None
        assert execution is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert conversation.pending_action_payload is not None
        assert conversation.pending_action_payload["cancellation_context"] == "provided"
        assert execution.sanitized_arguments["cancellation_context"] == "provided"
        assert cancellation_context not in str(execution.sanitized_arguments)
        assert cancellation_context not in str(conversation.pending_action_payload)
        assert cancellation_context not in str(audit_events)


@pytest.mark.parametrize(
    "cancellation_context",
    [
        "customer requested cancellation",
        "moving to Zamalek",
        "please cancel, call me later",
        "customer@example.com",
        "12 Garden Road",
        "call me at 12345",
    ],
)
async def test_prepare_cancel_persists_only_controlled_context_marker(
    session_factory, caplog: pytest.LogCaptureFixture, cancellation_context: str
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    key = f"prepare-cancel-privacy-{uuid.uuid4()}"

    result = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            cancellation_context=cancellation_context,
            idempotency_key=key,
        ),
    )

    assert isinstance(result, PreparationResult)
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )
        audit_events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.conversation_id == conversation_id)
            )
        ).all()
        assert conversation is not None
        assert execution is not None
        assert conversation.pending_action_payload is not None
        assert execution.sanitized_arguments["cancellation_context"] == "provided"
        assert conversation.pending_action_payload["cancellation_context"] == "provided"
        assert cancellation_context not in str(execution.sanitized_arguments)
        assert cancellation_context not in str(conversation.pending_action_payload)
        assert cancellation_context not in str(audit_events)
    assert cancellation_context not in caplog.text


async def test_prepare_reschedule_rejects_phase_one_reference_free_booking(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    async with session_factory.begin() as session:
        booking = Booking(
            contact_id=contact_id,
            service_id=service_id,
            status=BookingStatus.PENDING,
            start_at=REQUESTED_START,
            end_at=REQUESTED_END,
            booking_data={},
        )
        session.add(booking)
        await session.flush()
        booking_id = booking.id

    result = await prepare_reschedule_booking(
        session_factory,
        _calendar_double(),
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START,
            idempotency_key=f"prepare-primitive-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code in {
        BookingErrorCode.BOOKING_STATE_CONFLICT,
        BookingErrorCode.EXTERNAL_REFERENCE_MISSING,
    }


async def test_prepare_operations_never_call_calendar_mutation_methods(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    calendar = _calendar_double()
    create_result = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-no-mutation-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(create_result, PreparationResult)
    assert calendar.mutation_calls == []

    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START + timedelta(hours=1),
            idempotency_key=f"prepare-reschedule-no-mutation-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert calendar.mutation_calls == []


def test_deterministic_calendar_event_id_is_stable_and_google_safe() -> None:
    booking_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

    event_id = _deterministic_calendar_event_id(booking_id)

    assert event_id == _deterministic_calendar_event_id(booking_id)
    assert event_id.startswith("receptionist-booking-")
    assert len(event_id) <= 1024
    assert all(
        character.islower() or character.isdigit() or character in "-_"
        for character in event_id
    )
    assert str(booking_id) not in event_id


async def test_confirm_create_reconciles_booking_and_clears_action(session_factory) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-confirm-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    assert calendar.mutation_calls == []
    confirmation_key = f"confirm-create-{uuid.uuid4()}"

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=preparation.data.action_type,
            action_token=preparation.data.action_token,
            idempotency_key=confirmation_key,
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert result.data.status is BookingStatus.CONFIRMED
    assert len(calendar.mutation_calls) == 1
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == confirmation_key)
        )
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        ) if conversation is not None else None
        audits = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.conversation_id == conversation_id)
            )
        ).all()
        assert conversation is not None
        assert execution is not None
        assert booking is not None
        assert execution.status is ToolExecutionStatus.SUCCEEDED
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.calendar_id == CALENDAR_ID
        assert booking.calendar_event_id == result.data.calendar_event_id
        assert conversation.pending_action_type is None
        assert conversation.pending_action_payload is None
        assert audits
        event = await calendar.get_event(CALENDAR_ID, booking.calendar_event_id)
        assert event is not None
        assert event.private_booking_id == str(booking.id)


async def test_confirm_create_same_key_replays_without_second_calendar_event(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-replay-confirm-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-replay-{uuid.uuid4()}",
    )

    first = await confirm_booking_action(
        session_factory, calendar, request, now_utc=NOW_UTC
    )
    replay = await confirm_booking_action(
        session_factory, calendar, request, now_utc=NOW_UTC
    )

    assert isinstance(first, ConfirmationResult)
    assert isinstance(replay, ConfirmationResult)
    assert replay.replayed is True
    assert replay.data == first.data
    assert [call[0] for call in calendar.mutation_calls] == ["create"]


@pytest.mark.parametrize(
    "action_type,action_token",
    [
        (PendingActionType.CREATE_BOOKING, "wrong-token"),
        (PendingActionType.CANCEL_BOOKING, "wrong-token"),
    ],
)
async def test_confirm_mismatch_preserves_current_action_and_never_mutates_calendar(
    session_factory, action_type: PendingActionType, action_token: str
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-mismatch-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    key = f"confirm-mismatch-{uuid.uuid4()}"
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=action_type,
        action_token=action_token,
        idempotency_key=key,
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    replay = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.STALE_PENDING_ACTION
    assert isinstance(replay, BookingErrorResult)
    assert isinstance(replay, BookingErrorResult)
    assert replay.error == result.error
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )
        assert conversation is not None
        assert execution is not None
        assert conversation.pending_action_type is PendingActionType.CREATE_BOOKING
        assert conversation.pending_action_status is PendingActionStatus.AWAITING_CONFIRMATION
        assert conversation.pending_action_payload is not None
        assert conversation.pending_action_payload["action_token"] == preparation.data.action_token
        assert execution.status is ToolExecutionStatus.REJECTED


async def test_confirm_create_existing_event_with_foreign_marker_is_not_adopted(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(foreign_marker=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-foreign-marker-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=preparation.data.action_type,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-foreign-marker-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.EXTERNAL_STATE_CONFLICT
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        ) if conversation is not None else None
        assert conversation is not None
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        event = await calendar.get_event(CALENDAR_ID, booking.calendar_event_id)
        assert event is not None
        assert event.private_booking_id == "foreign-booking"


async def test_confirm_create_final_busy_cancels_reserved_local_intent(session_factory) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-busy-confirm-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    calendar._busy_intervals = (CalendarInterval(REQUESTED_START, REQUESTED_END),)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=preparation.data.action_type,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-busy-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_INTERVAL_UNAVAILABLE
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        ) if conversation is not None else None
        assert conversation is not None
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        assert booking.calendar_id is None
        assert booking.calendar_event_id is None
        assert conversation.pending_action_type is None


async def test_confirm_create_ambiguous_missing_event_retries_same_event_id(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(ambiguous_create_before_success=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-ambiguous-missing-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=preparation.data.action_type,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-ambiguous-missing-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert len(calendar.mutation_calls) == 2
    assert calendar.mutation_calls[0] == calendar.mutation_calls[1]


async def test_confirm_create_ambiguous_provider_success_reconciles_one_event(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(ambiguous_create_after_success=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-ambiguous-success-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-ambiguous-success-{uuid.uuid4()}",
    )

    first = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    replay = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(first, ConfirmationResult)
    assert isinstance(replay, ConfirmationResult)
    assert replay.replayed is True
    assert len(calendar.mutation_calls) == 1

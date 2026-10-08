from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from receptionist.application import booking as booking_application
from receptionist.application.booking import (
    AvailabilityRequest,
    BookingErrorCode,
    BookingErrorResult,
    BookingReadResult,
    CancelBookingRequest,
    ConfirmationResult,
    ConfirmBookingActionRequest,
    CreateBookingRequest,
    GetBookingRequest,
    PreparationResult,
    RescheduleBookingRequest,
    ToolExecutionClaimOutcome,
    _claim_tool_execution,
    _deterministic_calendar_event_id,
    check_availability,
    confirm_booking_action,
    get_booking,
    prepare_cancel_booking,
    prepare_create_booking,
    prepare_reschedule_booking,
)
from receptionist.db.models import (
    AuditEvent,
    Booking,
    BusinessConfig,
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
    CalendarEventLifecycle,
    CalendarEventSnapshot,
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


async def _conversation_for_contact(session_factory, contact_id: uuid.UUID) -> uuid.UUID:
    async with session_factory.begin() as session:
        conversation = Conversation(
            contact_id=contact_id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(conversation)
        await session.flush()
        return conversation.id


def _calendar_double(**kwargs) -> DeterministicCalendarDouble:
    return DeterministicCalendarDouble(calendar_id=CALENDAR_ID, **kwargs)


class _PreparationBarrierCalendar(DeterministicCalendarDouble):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__(calendar_id=CALENDAR_ID)
        self.barrier = barrier

    async def query_free_busy(self, calendar_id, time_min, time_max):
        await self.barrier.wait()
        return await super().query_free_busy(calendar_id, time_min, time_max)


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
    event_id = event_id or f"targetevent{uuid.uuid4().hex}"
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


async def _booking_snapshot(session_factory, booking_id: uuid.UUID) -> tuple[object, ...]:
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        tool_count = await session.scalar(select(func.count()).select_from(ToolExecution))
        audit_count = await session.scalar(select(func.count()).select_from(AuditEvent))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.contact_id == booking.contact_id)
        )
        assert conversation is not None
        return (
            booking.status,
            booking.start_at,
            booking.end_at,
            booking.calendar_id,
            booking.calendar_event_id,
            conversation.pending_action_type,
            conversation.pending_action_status,
            tool_count,
            audit_count,
        )


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


async def test_check_availability_uses_provider_authority_and_is_read_only(session_factory) -> None:
    await _operation_context(session_factory)
    calendar = _calendar_double()
    request = AvailabilityRequest(
        service_selector="plumbing",
        requested_start_at=REQUESTED_START,
        requested_end_at=REQUESTED_END,
    )
    async with session_factory() as session:
        before = (
            await session.scalar(select(func.count()).select_from(Booking)),
            await session.scalar(select(func.count()).select_from(ToolExecution)),
            await session.scalar(select(func.count()).select_from(AuditEvent)),
        )
        result = await check_availability(session, calendar, request, now_utc=NOW_UTC)
        after = (
            await session.scalar(select(func.count()).select_from(Booking)),
            await session.scalar(select(func.count()).select_from(ToolExecution)),
            await session.scalar(select(func.count()).select_from(AuditEvent)),
        )

    assert result.ok is True
    assert result.data.provider_available is True
    assert result.data.policy_valid is True
    assert result.data.requested_start_at_utc == REQUESTED_START
    assert result.data.requested_end_at_utc == REQUESTED_END
    assert result.data.read_at == NOW_UTC
    assert calendar.free_busy_queries == [
        (
            CALENDAR_ID,
            REQUESTED_START - timedelta(minutes=15),
            REQUESTED_END + timedelta(minutes=15),
        )
    ]
    assert calendar.mutation_calls == []
    assert after == before


async def test_check_availability_reports_provider_busy_without_local_authority(
    session_factory,
) -> None:
    await _operation_context(session_factory)
    calendar = _calendar_double(busy_intervals=(CalendarInterval(REQUESTED_START, REQUESTED_END),))
    request = AvailabilityRequest(
        service_selector="plumbing",
        requested_start_at=REQUESTED_START,
        requested_end_at=REQUESTED_END,
    )
    async with session_factory() as session:
        result = await check_availability(session, calendar, request, now_utc=NOW_UTC)
    assert result.ok is True
    assert result.data.policy_valid is True
    assert result.data.provider_available is False


async def test_check_availability_reports_policy_invalid_without_provider_read(
    session_factory,
) -> None:
    await _operation_context(session_factory)
    calendar = _calendar_double()
    request = AvailabilityRequest(
        service_selector="plumbing",
        requested_start_at=datetime(2026, 10, 11, 23, tzinfo=UTC),
        requested_end_at=datetime(2026, 10, 12, 0, tzinfo=UTC),
    )
    async with session_factory() as session:
        result = await check_availability(session, calendar, request, now_utc=NOW_UTC)
    assert result.ok is True
    assert result.data.policy_valid is False
    assert result.data.provider_available is False
    assert calendar.free_busy_queries == []


@pytest.mark.parametrize(
    "mutate,code",
    [
        ("inactive", BookingErrorCode.SERVICE_INACTIVE),
        ("not_bookable", BookingErrorCode.SERVICE_NOT_BOOKABLE),
    ],
)
async def test_check_availability_rejects_unavailable_service(
    session_factory, mutate: str, code: BookingErrorCode
) -> None:
    _, _, service_id = await _operation_context(session_factory)
    async with session_factory.begin() as session:
        service = await session.get(Service, service_id)
        assert service is not None
        if mutate == "inactive":
            service.active = False
        else:
            service.bookable = False
    async with session_factory() as session:
        result = await check_availability(
            session,
            _calendar_double(),
            AvailabilityRequest(service_selector="plumbing", requested_start_at=REQUESTED_START),
            now_utc=NOW_UTC,
        )
    assert isinstance(result, BookingErrorResult)
    assert result.error.code is code


async def test_check_availability_bounds_calendar_failure(session_factory) -> None:
    await _operation_context(session_factory)
    calendar = _calendar_double(
        free_busy_error=CalendarClientError(
            CalendarErrorCode.CALENDAR_UNAVAILABLE,
            "provider body must not escape",
            retryable=True,
        )
    )
    async with session_factory() as session:
        result = await check_availability(
            session,
            calendar,
            AvailabilityRequest(service_selector="plumbing", requested_start_at=REQUESTED_START),
            now_utc=NOW_UTC,
        )
    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider body" not in result.model_dump_json()


async def _seed_provider_event(
    calendar: DeterministicCalendarDouble,
    *,
    booking_id: uuid.UUID,
    calendar_id: str,
    event_id: str,
    interval: CalendarInterval,
) -> None:
    await calendar.create_event(
        calendar_id,
        event_id,
        CalendarEventCreate(interval=interval, private_booking_id=str(booking_id)),
    )
    calendar.mutation_calls.clear()


async def test_get_booking_returns_in_sync_provider_truth_without_mutation(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    await _seed_provider_event(
        calendar,
        booking_id=booking_id,
        calendar_id=CALENDAR_ID,
        event_id=(await _booking_event_id(session_factory, booking_id)),
        interval=CalendarInterval(REQUESTED_START, REQUESTED_END),
    )
    before = await _booking_snapshot(session_factory, booking_id)
    async with session_factory() as session:
        result = await get_booking(
            session,
            calendar,
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    after = await _booking_snapshot(session_factory, booking_id)
    assert isinstance(result, BookingReadResult)
    assert result.data.reconciliation_status == "in_sync"
    assert result.data.calendar_id == CALENDAR_ID
    assert result.data.calendar_event_id is not None
    assert before == after
    assert calendar.mutation_calls == []


async def test_get_booking_hides_another_contact_booking(session_factory) -> None:
    contact_id, _, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    _, other_conversation_id, _ = await _operation_context(session_factory)
    async with session_factory() as session:
        result = await get_booking(
            session,
            _calendar_double(),
            GetBookingRequest(conversation_id=other_conversation_id, booking_id=booking_id),
        )
    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.BOOKING_NOT_FOUND
    assert str(booking_id) not in result.model_dump_json()


async def test_get_booking_reports_provider_interval_marker_and_lifecycle_divergence(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    event_id = await _booking_event_id(session_factory, booking_id)

    for replacement in (
        replace(
            CalendarEventSnapshot(
                calendar_id=CALENDAR_ID,
                event_id=event_id,
                interval=CalendarInterval(
                    REQUESTED_START + timedelta(hours=1), REQUESTED_END + timedelta(hours=1)
                ),
                private_booking_id=str(booking_id),
                etag="etag-divergent",
            )
        ),
        CalendarEventSnapshot(
            calendar_id=CALENDAR_ID,
            event_id=event_id,
            interval=CalendarInterval(REQUESTED_START, REQUESTED_END),
            private_booking_id="foreign-booking",
            etag="etag-divergent",
        ),
        CalendarEventSnapshot(
            calendar_id=CALENDAR_ID,
            event_id=event_id,
            interval=None,
            private_booking_id=None,
            etag=None,
            lifecycle=CalendarEventLifecycle.CANCELLED,
        ),
    ):
        calendar = _calendar_double()
        calendar._events[(CALENDAR_ID, event_id)] = replacement
        async with session_factory() as session:
            result = await get_booking(
                session,
                calendar,
                GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
            )
        assert isinstance(result, BookingReadResult)
        assert result.data.reconciliation_status == "provider_divergent"


async def test_get_booking_reports_missing_confirmed_event(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    async with session_factory() as session:
        result = await get_booking(
            session,
            _calendar_double(),
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(result, BookingReadResult)
    assert result.data.reconciliation_status == "provider_missing"


async def test_get_booking_cancelled_reference_absence_and_tombstone_are_in_sync(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    event_id = await _booking_event_id(session_factory, booking_id)
    async with session_factory.begin() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = NOW_UTC
    async with session_factory() as session:
        absent = await get_booking(
            session,
            _calendar_double(),
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(absent, BookingReadResult)
    assert absent.data.reconciliation_status == "in_sync"

    tombstone_calendar = _calendar_double()
    tombstone_calendar._events[(CALENDAR_ID, event_id)] = CalendarEventSnapshot(
        calendar_id=CALENDAR_ID,
        event_id=event_id,
        interval=None,
        private_booking_id=None,
        etag=None,
        lifecycle=CalendarEventLifecycle.CANCELLED,
    )
    async with session_factory() as session:
        tombstone = await get_booking(
            session,
            tombstone_calendar,
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(tombstone, BookingReadResult)
    assert tombstone.data.reconciliation_status == "in_sync"


async def test_get_booking_reports_started_confirmation_without_provider_read(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    async with session_factory.begin() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        booking.status = BookingStatus.PENDING
        session.add(
            ToolExecution(
                conversation_id=conversation_id,
                tool_name="confirm_booking_action",
                status=ToolExecutionStatus.STARTED,
                idempotency_key=f"started-read-{uuid.uuid4()}",
                sanitized_arguments={},
                sanitized_result={"booking_id": str(booking_id)},
            )
        )
    calendar = _calendar_double()
    async with session_factory() as session:
        result = await get_booking(
            session,
            calendar,
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(result, BookingReadResult)
    assert result.data.reconciliation_status == "operation_in_progress"
    assert calendar.get_event_calls == []


async def test_get_booking_rejects_phase_one_reference_free_booking(session_factory) -> None:
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
    async with session_factory() as session:
        result = await get_booking(
            session,
            _calendar_double(),
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.EXTERNAL_REFERENCE_MISSING


async def test_get_booking_bounds_provider_read_failure_and_uses_persisted_identity(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = DeterministicCalendarDouble(
        calendar_id="new-configured-calendar",
        get_event_errors=(
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider response must not escape",
                retryable=True,
            ),
        ),
    )
    event_id = await _booking_event_id(session_factory, booking_id)
    async with session_factory() as session:
        result = await get_booking(
            session,
            calendar,
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )
    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider response" not in result.model_dump_json()
    assert calendar.get_event_calls == [(CALENDAR_ID, event_id)]


async def _booking_event_id(session_factory, booking_id: uuid.UUID) -> str:
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None and booking.calendar_event_id is not None
        return booking.calendar_event_id


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


async def test_confirmation_rejects_nonexistent_conversation_without_execution(
    session_factory,
) -> None:
    conversation_id = uuid.uuid4()
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.CREATE_BOOKING,
        action_token=str(uuid.uuid4()),
        idempotency_key=f"confirm-missing-conversation-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(
        session_factory,
        _calendar_double(),
        request,
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.INVALID_INPUT
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(ToolExecution.idempotency_key == request.idempotency_key)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.conversation_id == conversation_id)
            )
            == 0
        )


async def test_independent_preparations_do_not_serialize_on_business_write_fence(
    session_factory,
) -> None:
    _, first_conversation_id, _ = await _operation_context(session_factory)
    _, second_conversation_id, _ = await _operation_context(session_factory)
    calendar = _PreparationBarrierCalendar(asyncio.Barrier(2))

    results = await asyncio.wait_for(
        asyncio.gather(
            prepare_create_booking(
                session_factory,
                calendar,
                _create_request(first_conversation_id, f"prepare-read-a-{uuid.uuid4()}"),
                now_utc=NOW_UTC,
            ),
            prepare_create_booking(
                session_factory,
                calendar,
                _create_request(second_conversation_id, f"prepare-read-b-{uuid.uuid4()}"),
                now_utc=NOW_UTC,
            ),
        ),
        timeout=5,
    )

    assert all(isinstance(result, PreparationResult) for result in results)
    assert len(calendar.free_busy_queries) == 2


def test_deterministic_calendar_event_id_is_stable_and_google_safe() -> None:
    booking_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    other_booking_id = uuid.UUID("12345678-1234-5678-1234-567812345679")

    event_id = _deterministic_calendar_event_id(booking_id)
    expected_suffix = base64.b32hexencode(booking_id.bytes).decode("ascii").rstrip("=").lower()

    assert event_id == _deterministic_calendar_event_id(booking_id)
    assert event_id != _deterministic_calendar_event_id(other_booking_id)
    assert event_id == f"receptionistbooking{expected_suffix}"
    assert 5 <= len(event_id) <= 1024
    assert all(character in "0123456789abcdefghijklmnopqrstuv" for character in event_id)
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
        booking = (
            await session.scalar(
                select(Booking).where(Booking.contact_id == conversation.contact_id)
            )
            if conversation is not None
            else None
        )
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

    first = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    replay = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

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
        booking = (
            await session.scalar(
                select(Booking).where(Booking.contact_id == conversation.contact_id)
            )
            if conversation is not None
            else None
        )
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
        booking = (
            await session.scalar(
                select(Booking).where(Booking.contact_id == conversation.contact_id)
            )
            if conversation is not None
            else None
        )
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


async def test_confirm_create_recovers_after_crash_following_provider_success(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(crash_after_create_success=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-crash-recovery-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-crash-recovery-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        assert booking is not None
        assert execution is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED
        assert booking.calendar_event_id is not None
        assert await calendar.get_event(CALENDAR_ID, booking.calendar_event_id) is not None
        booking_id = booking.id

    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, ConfirmationResult)
    assert len(calendar.mutation_calls) == 1
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CONFIRMED
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        assert execution is not None
        assert execution.status is ToolExecutionStatus.SUCCEEDED
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Booking)
                .where(Booking.contact_id == booking.contact_id)
            )
            == 1
        )


async def test_create_recovery_reconciles_provider_before_changed_policy(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(crash_after_create_success=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-policy-recovery-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-policy-recovery-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    async with session_factory.begin() as session:
        business = await session.get(BusinessConfig, 1)
        assert business is not None
        original_policy = dict(business.booking_policy)
        business.booking_policy = {
            **business.booking_policy,
            "minimum_notice_minutes": 999999,
        }
    calendar.mutation_calls.clear()

    try:
        recovered = await confirm_booking_action(
            session_factory, calendar, request, now_utc=NOW_UTC
        )
        assert isinstance(recovered, ConfirmationResult)
        assert calendar.mutation_calls == []
    finally:
        async with session_factory.begin() as session:
            business = await session.get(BusinessConfig, 1)
            assert business is not None
            business.booking_policy = original_policy


async def test_create_recovery_uses_persisted_calendar_after_configuration_change(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(crash_after_create_success=True)
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-calendar-recovery-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-calendar-recovery-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()

    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, ConfirmationResult)
    assert calendar.mutation_calls == []


async def test_create_recovery_absent_event_and_changed_calendar_rejects_without_create(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(
        create_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-calendar-mismatch-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-calendar-mismatch-{uuid.uuid4()}",
    )

    first = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    assert isinstance(first, BookingErrorResult)
    assert first.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.EXTERNAL_STATE_CONFLICT
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        assert booking.calendar_id is None
        assert booking.calendar_event_id is None
        assert conversation.pending_action_type is None


async def test_create_initial_reconciliation_read_failure_is_bounded_and_recoverable(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    provider_error = CalendarClientError(
        CalendarErrorCode.CALENDAR_UNAVAILABLE, "provider-body", retryable=True
    )
    calendar = _calendar_double(get_event_errors=[provider_error])
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-read-failure-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-read-failure-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        assert booking is not None
        assert execution is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_create_ambiguous_reconciliation_read_failure_stays_started(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    provider_error = CalendarClientError(
        CalendarErrorCode.CALENDAR_UNAVAILABLE, "provider-body", retryable=True
    )
    calendar = _calendar_double(
        get_event_errors=[None, provider_error], ambiguous_create_after_success=True
    )
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-ambiguous-read-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-ambiguous-read-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    assert len(calendar.mutation_calls) == 1
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        assert booking is not None
        assert execution is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_create_retryable_calendar_error_stays_recoverable(session_factory) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(
        create_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-calendar-unavailable-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-calendar-unavailable-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        assert booking is not None
        assert booking.status is BookingStatus.PENDING
        assert booking.calendar_id == CALENDAR_ID
        assert booking.calendar_event_id is not None
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_create_nonretryable_calendar_error_cancels_uncreated_intent(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double(
        create_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=False,
            )
        ]
    )
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-create-calendar-rejected-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-create-calendar-rejected-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is False
    assert "provider-body" not in str(result)
    assert len(calendar.mutation_calls) == 1
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        booking = await session.scalar(
            select(Booking).where(Booking.contact_id == conversation.contact_id)
        )
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        assert booking.calendar_id is None
        assert booking.calendar_event_id is None
        assert conversation.pending_action_type is None


async def _seed_calendar_event_for_booking(
    session_factory, calendar: DeterministicCalendarDouble, booking_id: uuid.UUID
) -> str:
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.calendar_event_id is not None
        assert booking.start_at is not None
        assert booking.end_at is not None
        event_id = booking.calendar_event_id
        interval = CalendarInterval(booking.start_at, booking.end_at)
    await calendar.create_event(
        CALENDAR_ID,
        event_id,
        CalendarEventCreate(interval=interval, private_booking_id=str(booking_id)),
    )
    calendar.mutation_calls.clear()
    return event_id


async def test_reschedule_initial_reconciliation_read_failure_is_bounded(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    provider_error = CalendarClientError(
        CalendarErrorCode.CALENDAR_UNAVAILABLE, "provider-body", retryable=True
    )
    calendar = _calendar_double(get_event_errors=[provider_error])
    await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START + timedelta(hours=1),
            idempotency_key=f"prepare-reschedule-read-failure-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.RESCHEDULE_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-reschedule-read-failure-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert execution is not None
        assert conversation is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_reschedule_ambiguous_patch_reconciliation_read_failure_stays_started(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    provider_error = CalendarClientError(
        CalendarErrorCode.CALENDAR_UNAVAILABLE, "provider-body", retryable=True
    )
    calendar = _calendar_double(
        get_event_errors=[None, provider_error], ambiguous_patch_after_success=True
    )
    await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START + timedelta(hours=1),
            idempotency_key=f"prepare-reschedule-ambiguous-read-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.RESCHEDULE_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-reschedule-ambiguous-read-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert execution is not None
        assert conversation is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_reschedule_retryable_calendar_error_preserves_started_state(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        patch_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-calendar-unavailable-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-reschedule-calendar-unavailable-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    assert event.interval == CalendarInterval(REQUESTED_START, REQUESTED_END)
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.PENDING
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_reschedule_nonretryable_calendar_error_restores_confirmed_state(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        patch_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=False,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-calendar-rejected-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-reschedule-calendar-rejected-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is False
    assert "provider-body" not in str(result)
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    assert event.interval == CalendarInterval(REQUESTED_START, REQUESTED_END)
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == REQUESTED_START
        assert booking.end_at == REQUESTED_END
        assert conversation.pending_action_type is None


async def test_cancel_retryable_calendar_error_preserves_started_state(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        cancel_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-calendar-unavailable-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-calendar-unavailable-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.PENDING
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_cancel_nonretryable_calendar_error_restores_confirmed_state(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        cancel_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=False,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-calendar-rejected-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-calendar-rejected-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE
    assert result.error.retryable is False
    assert "provider-body" not in str(result)
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert conversation.pending_action_type is None


async def test_cancel_reconciliation_read_failure_stays_started(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    provider_error = CalendarClientError(
        CalendarErrorCode.CALENDAR_UNAVAILABLE, "provider-body", retryable=True
    )
    calendar = _calendar_double(
        get_event_errors=[None, provider_error], ambiguous_cancel_after_success=True
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-ambiguous-read-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.CANCEL_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-cancel-ambiguous-read-{uuid.uuid4()}",
    )

    result = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert result.error.retryable is True
    assert "provider-body" not in str(result)
    assert await calendar.get_event(CALENDAR_ID, event_id) is None
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert execution is not None
        assert conversation is not None
        assert booking.status is BookingStatus.PENDING
        assert execution.status is ToolExecutionStatus.STARTED
        assert conversation.pending_action_status is PendingActionStatus.CONFIRMED


async def test_confirm_reschedule_patches_only_owned_interval_and_reconciles(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-confirm-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-reschedule-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert result.data.status is BookingStatus.CONFIRMED
    assert calendar.mutation_calls == [("patch", CALENDAR_ID, event_id)]
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    assert event.interval == CalendarInterval(requested_start, requested_start + timedelta(hours=1))
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == requested_start
        assert booking.end_at == requested_start + timedelta(hours=1)


async def test_confirm_cancel_deletes_event_and_retains_historical_references(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-confirm-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert result.data.status is BookingStatus.CANCELLED
    assert calendar.mutation_calls == [("cancel", CALENDAR_ID, event_id)]
    assert await calendar.get_event(CALENDAR_ID, event_id) is None
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        assert booking.calendar_id == CALENDAR_ID
        assert booking.calendar_event_id == event_id
        assert booking.cancelled_at is not None


async def test_confirm_cancel_already_absent_event_is_reconciled_without_delete(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-absent-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-absent-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert result.data.status is BookingStatus.CANCELLED
    assert calendar.mutation_calls == []


async def test_confirm_cancel_matching_cancelled_event_is_reconciled_without_delete(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    calendar._events[(CALENDAR_ID, event_id)] = replace(
        event,
        lifecycle=CalendarEventLifecycle.CANCELLED,
        interval=None,
        private_booking_id=None,
        etag=None,
    )
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-cancelled-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-cancelled-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert result.data.status is BookingStatus.CANCELLED
    assert calendar.mutation_calls == []


async def test_confirm_reschedule_rejects_stale_fingerprint_without_patch(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START + timedelta(hours=1),
            idempotency_key=f"prepare-reschedule-stale-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    async with session_factory.begin() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        booking.booking_data = {**booking.booking_data, "requirements": {"changed": "yes"}}

    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-reschedule-stale-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, BookingErrorResult)
    assert result.error.code is BookingErrorCode.STALE_PENDING_ACTION
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert conversation.pending_action_type is None


async def test_confirm_reschedule_ambiguous_patch_reconciles_desired_state(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(ambiguous_patch_after_success=True)
    await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-ambiguous-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-reschedule-ambiguous-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert calendar.mutation_calls == [("patch", CALENDAR_ID, next(iter(calendar._events))[1])]


async def test_reschedule_recovery_trusts_already_applied_provider_state(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(crash_after_patch_success=True)
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    requested_end = requested_start + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-crash-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.RESCHEDULE_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-reschedule-crash-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash after Calendar patch"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    assert event.interval == CalendarInterval(requested_start, requested_end)
    await calendar.create_event(
        CALENDAR_ID,
        "blockingevent",
        CalendarEventCreate(
            interval=CalendarInterval(requested_start, requested_end),
            private_booking_id="other-booking",
        ),
    )
    async with session_factory.begin() as session:
        business = await session.get(BusinessConfig, 1)
        assert business is not None
        original_policy = dict(business.booking_policy)
        business.booking_policy = {
            **business.booking_policy,
            "minimum_notice_minutes": 999999,
        }
    calendar.mutation_calls.clear()

    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, ConfirmationResult)
    assert calendar.mutation_calls == []
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == requested_start
        assert booking.end_at == requested_end
    async with session_factory.begin() as session:
        business = await session.get(BusinessConfig, 1)
        assert business is not None
        business.booking_policy = original_policy


async def test_reschedule_recovery_reads_persisted_calendar_after_configuration_change(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(crash_after_patch_success=True)
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    requested_start = REQUESTED_START + timedelta(hours=1)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=requested_start,
            idempotency_key=f"prepare-reschedule-calendar-recovery-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.RESCHEDULE_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-reschedule-calendar-recovery-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash after Calendar patch"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()
    calendar.get_event_calls.clear()

    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, ConfirmationResult)
    assert calendar.mutation_calls == []
    assert calendar.get_event_calls == [(CALENDAR_ID, event_id)]
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == requested_start


async def test_reschedule_prior_state_and_changed_calendar_restores_without_patch(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        patch_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=REQUESTED_START + timedelta(hours=1),
            idempotency_key=f"prepare-reschedule-calendar-prior-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.RESCHEDULE_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-reschedule-calendar-prior-{uuid.uuid4()}",
    )

    first = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    assert isinstance(first, BookingErrorResult)
    assert first.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE

    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()
    calendar.get_event_calls.clear()
    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, BookingErrorResult)
    assert recovered.error.code is BookingErrorCode.EXTERNAL_STATE_CONFLICT
    assert calendar.mutation_calls == []
    assert calendar.get_event_calls == [(CALENDAR_ID, event_id)]
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert booking.start_at == REQUESTED_START
        assert conversation.pending_action_type is None


async def test_confirm_cancel_ambiguous_delete_reconciles_absent_event(session_factory) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(ambiguous_cancel_after_success=True)
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-ambiguous-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)
    result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=preparation.data.action_token,
            idempotency_key=f"confirm-cancel-ambiguous-{uuid.uuid4()}",
        ),
        now_utc=NOW_UTC,
    )

    assert isinstance(result, ConfirmationResult)
    assert calendar.mutation_calls == [("cancel", CALENDAR_ID, event_id)]


async def test_cancel_recovery_reads_persisted_calendar_after_configuration_change(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(crash_after_cancel_success=True)
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-calendar-recovery-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.CANCEL_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-cancel-calendar-recovery-{uuid.uuid4()}",
    )

    with pytest.raises(RuntimeError, match="simulated crash after Calendar cancel"):
        await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()
    calendar.get_event_calls.clear()
    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, ConfirmationResult)
    assert calendar.mutation_calls == []
    assert calendar.get_event_calls == [(CALENDAR_ID, event_id)]
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        assert booking is not None
        assert booking.status is BookingStatus.CANCELLED
        assert booking.calendar_id == CALENDAR_ID
        assert booking.calendar_event_id == event_id


async def test_cancel_active_event_and_changed_calendar_restores_without_delete(
    session_factory,
) -> None:
    contact_id, conversation_id, service_id = await _operation_context(session_factory)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double(
        cancel_errors=[
            CalendarClientError(
                CalendarErrorCode.CALENDAR_UNAVAILABLE,
                "provider-body",
                retryable=True,
            )
        ]
    )
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"prepare-cancel-calendar-active-{uuid.uuid4()}",
        ),
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=PendingActionType.CANCEL_BOOKING,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-cancel-calendar-active-{uuid.uuid4()}",
    )

    first = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    assert isinstance(first, BookingErrorResult)
    assert first.error.code is BookingErrorCode.CALENDAR_UNAVAILABLE

    calendar.calendar_id = "new-default-calendar"
    calendar.mutation_calls.clear()
    calendar.get_event_calls.clear()
    recovered = await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    assert isinstance(recovered, BookingErrorResult)
    assert recovered.error.code is BookingErrorCode.EXTERNAL_STATE_CONFLICT
    assert calendar.mutation_calls == []
    assert calendar.get_event_calls == [(CALENDAR_ID, event_id)]
    async with session_factory() as session:
        booking = await session.get(Booking, booking_id)
        conversation = await session.get(Conversation, conversation_id)
        assert booking is not None
        assert conversation is not None
        assert booking.status is BookingStatus.CONFIRMED
        assert conversation.pending_action_type is None


async def test_concurrent_same_key_confirmations_create_one_booking_and_event(
    session_factory,
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-same-key-race-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-same-key-race-{uuid.uuid4()}",
    )
    barrier = asyncio.Barrier(2)

    async def contender() -> ConfirmationResult | BookingErrorResult:
        await barrier.wait()
        return await confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)

    results = await asyncio.wait_for(asyncio.gather(contender(), contender()), timeout=5)

    assert sum(isinstance(result, ConfirmationResult) for result in results) == 2
    assert sum(getattr(result, "replayed", False) for result in results) == 1
    assert calendar.mutation_calls == [("create", CALENDAR_ID, calendar.mutation_calls[0][2])]
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Booking)
                .where(Booking.contact_id == conversation.contact_id)
            )
            == 1
        )


async def test_same_key_confirmation_forced_lock_interleaving_cannot_deadlock(
    session_factory, monkeypatch
) -> None:
    _, conversation_id, _ = await _operation_context(session_factory)
    calendar = _calendar_double()
    preparation = await prepare_create_booking(
        session_factory,
        calendar,
        _create_request(conversation_id, f"prepare-lock-order-{uuid.uuid4()}"),
        now_utc=NOW_UTC,
    )
    assert isinstance(preparation, PreparationResult)
    request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=preparation.data.action_type,
        action_token=preparation.data.action_token,
        idempotency_key=f"confirm-lock-order-{uuid.uuid4()}",
    )
    domain_rows_locked = asyncio.Event()
    second_claim_attempted = asyncio.Event()
    release_domain_rows = asyncio.Event()
    original_lock = booking_application._lock_ordered_rows
    original_claim = booking_application._claim_tool_execution
    lock_count = 0
    claim_count = 0

    async def coordinated_lock(*args, **kwargs):
        nonlocal lock_count
        lock_count += 1
        result = await original_lock(*args, **kwargs)
        if lock_count == 2:
            domain_rows_locked.set()
            await release_domain_rows.wait()
        return result

    async def coordinated_claim(*args, **kwargs):
        nonlocal claim_count
        claim_count += 1
        if claim_count == 2:
            second_claim_attempted.set()
        return await original_claim(*args, **kwargs)

    monkeypatch.setattr(booking_application, "_lock_ordered_rows", coordinated_lock)
    monkeypatch.setattr(booking_application, "_claim_tool_execution", coordinated_claim)

    first = asyncio.create_task(
        confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    )
    await asyncio.wait_for(domain_rows_locked.wait(), timeout=5)
    second = asyncio.create_task(
        confirm_booking_action(session_factory, calendar, request, now_utc=NOW_UTC)
    )
    await asyncio.wait_for(second_claim_attempted.wait(), timeout=5)
    release_domain_rows.set()

    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert all(isinstance(result, ConfirmationResult) for result in results)
    assert sum(result.replayed for result in results if isinstance(result, ConfirmationResult)) == 1
    assert len(calendar.mutation_calls) == 1


async def test_concurrent_exact_duplicate_creates_share_contact_without_deadlock(
    session_factory,
) -> None:
    contact_id, first_conversation_id, _ = await _operation_context(session_factory)
    second_conversation_id = await _conversation_for_contact(session_factory, contact_id)
    calendar = _calendar_double()
    first_preparation, second_preparation = await asyncio.gather(
        prepare_create_booking(
            session_factory,
            calendar,
            _create_request(first_conversation_id, f"prepare-duplicate-a-{uuid.uuid4()}"),
            now_utc=NOW_UTC,
        ),
        prepare_create_booking(
            session_factory,
            calendar,
            _create_request(second_conversation_id, f"prepare-duplicate-b-{uuid.uuid4()}"),
            now_utc=NOW_UTC,
        ),
    )
    assert isinstance(first_preparation, PreparationResult)
    assert isinstance(second_preparation, PreparationResult)
    barrier = asyncio.Barrier(2)

    async def contender(
        conversation_id: uuid.UUID, preparation: PreparationResult
    ) -> ConfirmationResult | BookingErrorResult:
        await barrier.wait()
        return await confirm_booking_action(
            session_factory,
            calendar,
            ConfirmBookingActionRequest(
                conversation_id=conversation_id,
                action_type=preparation.data.action_type,
                action_token=preparation.data.action_token,
                idempotency_key=f"confirm-duplicate-{uuid.uuid4()}",
            ),
            now_utc=NOW_UTC,
        )

    results = await asyncio.wait_for(
        asyncio.gather(
            contender(first_conversation_id, first_preparation),
            contender(second_conversation_id, second_preparation),
        ),
        timeout=5,
    )

    assert sum(isinstance(result, ConfirmationResult) for result in results) == 1
    errors = [result for result in results if isinstance(result, BookingErrorResult)]
    assert len(errors) == 1
    assert errors[0].error.code is BookingErrorCode.DUPLICATE_REPLAY
    assert len(calendar.mutation_calls) == 1
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Booking).where(Booking.contact_id == contact_id)
            )
            == 1
        )


async def test_competing_reschedules_cannot_overwrite_each_other(session_factory) -> None:
    contact_id, first_conversation_id, service_id = await _operation_context(session_factory)
    second_conversation_id = await _conversation_for_contact(session_factory, contact_id)
    booking_id = await _confirmed_booking(
        session_factory, contact_id=contact_id, service_id=service_id
    )
    calendar = _calendar_double()
    event_id = await _seed_calendar_event_for_booking(session_factory, calendar, booking_id)
    first_start = REQUESTED_START + timedelta(hours=1)
    second_start = REQUESTED_START + timedelta(hours=2)
    first_preparation, second_preparation = await asyncio.gather(
        prepare_reschedule_booking(
            session_factory,
            calendar,
            RescheduleBookingRequest(
                conversation_id=first_conversation_id,
                booking_id=booking_id,
                requested_start_at=first_start,
                idempotency_key=f"prepare-reschedule-race-a-{uuid.uuid4()}",
            ),
            now_utc=NOW_UTC,
        ),
        prepare_reschedule_booking(
            session_factory,
            calendar,
            RescheduleBookingRequest(
                conversation_id=second_conversation_id,
                booking_id=booking_id,
                requested_start_at=second_start,
                idempotency_key=f"prepare-reschedule-race-b-{uuid.uuid4()}",
            ),
            now_utc=NOW_UTC,
        ),
    )
    assert isinstance(first_preparation, PreparationResult)
    assert isinstance(second_preparation, PreparationResult)
    barrier = asyncio.Barrier(2)

    async def contender(
        conversation_id: uuid.UUID, preparation: PreparationResult, key: str
    ) -> ConfirmationResult | BookingErrorResult:
        await barrier.wait()
        return await confirm_booking_action(
            session_factory,
            calendar,
            ConfirmBookingActionRequest(
                conversation_id=conversation_id,
                action_type=PendingActionType.RESCHEDULE_BOOKING,
                action_token=preparation.data.action_token,
                idempotency_key=key,
            ),
            now_utc=NOW_UTC,
        )

    results = await asyncio.wait_for(
        asyncio.gather(
            contender(
                first_conversation_id,
                first_preparation,
                f"confirm-reschedule-race-a-{uuid.uuid4()}",
            ),
            contender(
                second_conversation_id,
                second_preparation,
                f"confirm-reschedule-race-b-{uuid.uuid4()}",
            ),
        ),
        timeout=5,
    )

    assert sum(isinstance(result, ConfirmationResult) for result in results) == 1
    errors = [result for result in results if isinstance(result, BookingErrorResult)]
    assert len(errors) == 1
    assert errors[0].error.code in {
        BookingErrorCode.BOOKING_STATE_CONFLICT,
        BookingErrorCode.STALE_PENDING_ACTION,
    }
    assert calendar.mutation_calls == [("patch", CALENDAR_ID, event_id)]
    event = await calendar.get_event(CALENDAR_ID, event_id)
    assert event is not None
    assert event.interval in {
        CalendarInterval(first_start, first_start + timedelta(hours=1)),
        CalendarInterval(second_start, second_start + timedelta(hours=1)),
    }

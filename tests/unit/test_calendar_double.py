from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from receptionist.application.booking import check_calendar_availability
from receptionist.domain.booking_policy import PolicyDecision, RequestedInterval
from receptionist.integrations.google_calendar import (
    CalendarBusyInterval,
    CalendarClient,
    CalendarClientError,
    CalendarErrorCode,
    CalendarEventCreate,
    CalendarEventPatch,
    CalendarInterval,
)
from tests.support.calendar_double import DeterministicCalendarDouble

pytestmark = pytest.mark.unit

CALENDAR_ID = "calendar-id"
INTERVAL = CalendarInterval(
    start_at_utc=datetime(2026, 10, 11, 7, tzinfo=UTC),
    end_at_utc=datetime(2026, 10, 11, 8, tzinfo=UTC),
)


async def test_calendar_double_supports_free_busy_event_crud_and_etags() -> None:
    double = DeterministicCalendarDouble()
    created = await double.create_event(
        CALENDAR_ID,
        "event-id",
        CalendarEventCreate(interval=INTERVAL, private_booking_id="booking-id"),
    )

    assert created.event_id == "event-id"
    assert created.private_booking_id == "booking-id"
    assert await double.get_event(CALENDAR_ID, "event-id") == created

    patched = await double.patch_event(
        CALENDAR_ID,
        "event-id",
        CalendarEventPatch(
            interval=CalendarInterval(
                start_at_utc=INTERVAL.start_at_utc + timedelta(hours=1),
                end_at_utc=INTERVAL.end_at_utc + timedelta(hours=1),
            )
        ),
        created.etag,
    )
    assert patched.interval.start_at_utc == datetime(2026, 10, 11, 8, tzinfo=UTC)
    assert patched.etag != created.etag

    with pytest.raises(CalendarClientError) as exc_info:
        await double.patch_event(
            CALENDAR_ID,
            "event-id",
            CalendarEventPatch(interval=INTERVAL),
            created.etag,
        )
    assert exc_info.value.code is CalendarErrorCode.EXTERNAL_STATE_CONFLICT

    await double.cancel_event(CALENDAR_ID, "event-id", patched.etag)
    assert await double.get_event(CALENDAR_ID, "event-id") is None


def test_calendar_double_exposes_configured_calendar_id_contract() -> None:
    double = DeterministicCalendarDouble(calendar_id=CALENDAR_ID)
    client: CalendarClient = double

    assert client.calendar_id == CALENDAR_ID


async def test_calendar_double_supports_busy_and_conflict_reads() -> None:
    busy_interval = CalendarBusyInterval(INTERVAL.start_at_utc, INTERVAL.end_at_utc)
    double = DeterministicCalendarDouble(busy_intervals=(busy_interval,))
    assert await double.query_free_busy(
        CALENDAR_ID, INTERVAL.start_at_utc, INTERVAL.end_at_utc
    ) == (busy_interval,)

    blocker = await double.create_event(
        CALENDAR_ID,
        "blocker-id",
        CalendarEventCreate(interval=INTERVAL, private_booking_id="blocker-booking"),
    )
    conflicts = await double.query_conflicts(
        CALENDAR_ID, INTERVAL.start_at_utc, INTERVAL.end_at_utc
    )
    assert [conflict.event_id for conflict in conflicts] == [blocker.event_id]


async def test_calendar_double_ambiguous_create_persists_before_recoverable_error() -> None:
    double = DeterministicCalendarDouble(ambiguous_create_event_ids={"event-id"})

    with pytest.raises(CalendarClientError) as exc_info:
        await double.create_event(
            CALENDAR_ID,
            "event-id",
            CalendarEventCreate(interval=INTERVAL, private_booking_id="booking-id"),
        )

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    persisted = await double.get_event(CALENDAR_ID, "event-id")
    assert persisted is not None
    assert persisted.private_booking_id == "booking-id"


async def test_calendar_double_can_fail_closed_for_free_busy() -> None:
    double = DeterministicCalendarDouble(
        free_busy_error=CalendarClientError(
            CalendarErrorCode.CALENDAR_UNAVAILABLE,
            "calendar availability is unavailable",
        )
    )

    with pytest.raises(CalendarClientError) as exc_info:
        await double.query_free_busy(CALENDAR_ID, INTERVAL.start_at_utc, INTERVAL.end_at_utc)
    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE


def _policy(
    *, before_minutes: int = 0, after_minutes: int = 0, valid: bool = True
) -> PolicyDecision:
    requested = RequestedInterval(
        datetime(2026, 10, 11, 7, tzinfo=UTC),
        datetime(2026, 10, 11, 8, tzinfo=UTC),
    )
    effective = RequestedInterval(
        requested.start_at_utc - timedelta(minutes=before_minutes),
        requested.end_at_utc + timedelta(minutes=after_minutes),
    )
    return PolicyDecision(
        valid=valid,
        error_code=None if valid else "outside_business_policy",
        effective_interval=effective,
    )


async def test_calendar_backed_availability_allows_reschedule_self_overlap() -> None:
    target_id = "target-event"
    double = DeterministicCalendarDouble()
    target = await double.create_event(
        CALENDAR_ID,
        target_id,
        CalendarEventCreate(interval=INTERVAL, private_booking_id="target-booking"),
    )

    result = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
        exclude_event_id=target.event_id,
    )

    assert result.policy_valid is True
    assert result.provider_available is True
    assert result.error_code is None
    assert double.free_busy_queries == []
    assert len(double.conflict_queries) == 1
    assert double.conflict_queries[0][3] == target.event_id


async def test_calendar_backed_availability_allows_buffer_touching_only_self() -> None:
    double = DeterministicCalendarDouble()
    target = await double.create_event(
        CALENDAR_ID,
        "target-event",
        CalendarEventCreate(interval=INTERVAL, private_booking_id="target-booking"),
    )

    result = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(before_minutes=15),
        exclude_event_id=target.event_id,
    )

    assert result.provider_available is True


async def test_calendar_backed_availability_rejects_another_event_in_effective_interval() -> None:
    double = DeterministicCalendarDouble()
    target = await double.create_event(
        CALENDAR_ID,
        "target-event",
        CalendarEventCreate(interval=INTERVAL, private_booking_id="target-booking"),
    )
    await double.create_event(
        CALENDAR_ID,
        "other-event",
        CalendarEventCreate(
            interval=CalendarInterval(
                datetime(2026, 10, 11, 7, 30, tzinfo=UTC),
                datetime(2026, 10, 11, 8, 30, tzinfo=UTC),
            ),
            private_booking_id="other-booking",
        ),
    )

    result = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
        exclude_event_id=target.event_id,
    )

    assert result.provider_available is False
    assert result.error_code is None


async def test_calendar_backed_availability_uses_provider_reads_not_local_rows() -> None:
    double = DeterministicCalendarDouble(
        busy_intervals=(CalendarBusyInterval(INTERVAL.start_at_utc, INTERVAL.end_at_utc),)
    )
    result = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
    )

    assert result.provider_available is False
    assert result.error_code is None
    assert len(double.free_busy_queries) == 1
    assert double.conflict_queries == []


async def test_calendar_backed_availability_preserves_policy_and_provider_errors() -> None:
    unavailable = DeterministicCalendarDouble(
        free_busy_error=CalendarClientError(
            CalendarErrorCode.CALENDAR_UNAVAILABLE,
            "provider details must not escape",
        )
    )
    calendar_error = await check_calendar_availability(
        unavailable,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
    )
    assert calendar_error.policy_valid is True
    assert calendar_error.provider_available is False
    assert calendar_error.error_code == "calendar_unavailable"

    invalid = await check_calendar_availability(
        unavailable,
        calendar_id=CALENDAR_ID,
        policy=_policy(valid=False),
    )
    assert invalid.policy_valid is False
    assert invalid.provider_available is False
    assert invalid.error_code == "outside_business_policy"


async def test_calendar_backed_availability_excludes_only_exact_target_id() -> None:
    double = DeterministicCalendarDouble()
    target = await double.create_event(
        CALENDAR_ID,
        "target-event",
        CalendarEventCreate(interval=INTERVAL, private_booking_id="target-booking"),
    )

    different_exclusion = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
        exclude_event_id=str(uuid4()),
    )
    exact_exclusion = await check_calendar_availability(
        double,
        calendar_id=CALENDAR_ID,
        policy=_policy(),
        exclude_event_id=target.event_id,
    )

    assert different_exclusion.provider_available is False
    assert exact_exclusion.provider_available is True

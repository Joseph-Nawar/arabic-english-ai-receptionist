from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from receptionist.integrations.google_calendar import (
    CalendarBusyInterval,
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

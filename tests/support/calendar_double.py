"""One deterministic, provider-free implementation of the Calendar boundary."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from receptionist.integrations.google_calendar import (
    CalendarBusyInterval,
    CalendarClientError,
    CalendarConflict,
    CalendarErrorCode,
    CalendarEventCreate,
    CalendarEventLifecycle,
    CalendarEventPatch,
    CalendarEventSnapshot,
    CalendarInterval,
)


class DeterministicCalendarDouble:
    """A small in-memory Calendar double with bounded failure controls."""

    def __init__(
        self,
        *,
        calendar_id: str = "calendar-id",
        busy_intervals: Iterable[CalendarBusyInterval | CalendarInterval] = (),
        free_busy_error: CalendarClientError | None = None,
        ambiguous_create_event_ids: set[str] | None = None,
        ambiguous_create_before_success: bool = False,
        ambiguous_create_after_success: bool = False,
        crash_after_create_success: bool = False,
        create_errors: Iterable[CalendarClientError] = (),
        foreign_marker_event_ids: set[str] | None = None,
        foreign_marker: bool = False,
        ambiguous_patch_after_success: bool = False,
        crash_after_patch_success: bool = False,
        patch_errors: Iterable[CalendarClientError] = (),
        ambiguous_cancel_after_success: bool = False,
        crash_after_cancel_success: bool = False,
        cancel_errors: Iterable[CalendarClientError] = (),
        get_event_errors: Iterable[CalendarClientError | None] = (),
    ) -> None:
        self.calendar_id = calendar_id
        self._busy_intervals = tuple(busy_intervals)
        self._free_busy_error = free_busy_error
        self._ambiguous_create_event_ids = ambiguous_create_event_ids or set()
        self._ambiguous_create_before_success = ambiguous_create_before_success
        self._ambiguous_create_after_success = ambiguous_create_after_success
        self._crash_after_create_success = crash_after_create_success
        self._create_errors = list(create_errors)
        self._foreign_marker_event_ids = foreign_marker_event_ids or set()
        self._foreign_marker = foreign_marker
        self._ambiguous_patch_after_success = ambiguous_patch_after_success
        self._crash_after_patch_success = crash_after_patch_success
        self._patch_errors = list(patch_errors)
        self._ambiguous_cancel_after_success = ambiguous_cancel_after_success
        self._crash_after_cancel_success = crash_after_cancel_success
        self._cancel_errors = list(cancel_errors)
        self._get_event_errors = list(get_event_errors)
        self._events: dict[tuple[str, str], CalendarEventSnapshot] = {}
        self._etag_counter = 0
        self.free_busy_queries: list[tuple[str, datetime, datetime]] = []
        self.conflict_queries: list[tuple[str, datetime, datetime, str | None]] = []
        self.get_event_calls: list[tuple[str, str]] = []
        self.mutation_calls: list[tuple[str, str, str]] = []

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    @staticmethod
    def _overlaps(left: CalendarInterval, right: CalendarInterval) -> bool:
        return left.start_at_utc < right.end_at_utc and right.start_at_utc < left.end_at_utc

    async def query_free_busy(
        self, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> tuple[CalendarBusyInterval, ...]:
        self.free_busy_queries.append((calendar_id, time_min, time_max))
        if self._free_busy_error is not None:
            raise self._free_busy_error
        requested = CalendarInterval(time_min, time_max)
        return tuple(
            CalendarBusyInterval(interval.start_at_utc, interval.end_at_utc)
            for interval in self._busy_intervals
            if self._overlaps(
                requested, CalendarInterval(interval.start_at_utc, interval.end_at_utc)
            )
        )

    async def query_conflicts(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        exclude_event_id: str | None = None,
    ) -> tuple[CalendarConflict, ...]:
        self.conflict_queries.append((calendar_id, time_min, time_max, exclude_event_id))
        requested = CalendarInterval(time_min, time_max)
        conflicts = []
        for (event_calendar_id, event_id), event in self._events.items():
            if event_calendar_id != calendar_id or event_id == exclude_event_id:
                continue
            if event.interval is not None and self._overlaps(requested, event.interval):
                conflicts.append(CalendarConflict(event_id=event_id, interval=event.interval))
        return tuple(conflicts)

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventSnapshot | None:
        self.get_event_calls.append((calendar_id, event_id))
        if self._get_event_errors:
            error = self._get_event_errors.pop(0)
            if error is not None:
                raise error
        return self._events.get((calendar_id, event_id))

    async def create_event(
        self, calendar_id: str, event_id: str, event: CalendarEventCreate
    ) -> CalendarEventSnapshot:
        allowed_event_id_characters = "0123456789abcdefghijklmnopqrstuv"
        if not 5 <= len(event_id) <= 1024 or any(
            character not in allowed_event_id_characters for character in event_id
        ):
            raise ValueError("Calendar event ID is not valid for Google Calendar")
        self.mutation_calls.append(("create", calendar_id, event_id))
        key = (calendar_id, event_id)
        if self._ambiguous_create_before_success:
            self._ambiguous_create_before_success = False
            raise CalendarClientError(
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED,
                "Calendar write result requires reconciliation",
                retryable=True,
            )
        if self._create_errors:
            raise self._create_errors.pop(0)
        if key in self._events:
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_STATE_CONFLICT,
                "the Calendar event conflicts with existing state",
            )
        snapshot = CalendarEventSnapshot(
            calendar_id=calendar_id,
            event_id=event_id,
            interval=event.interval,
            private_booking_id=(
                "foreign-booking"
                if self._foreign_marker or event_id in self._foreign_marker_event_ids
                else event.private_booking_id
            ),
            etag=self._next_etag(),
        )
        self._events[key] = snapshot
        if self._crash_after_create_success:
            self._crash_after_create_success = False
            raise RuntimeError("simulated crash after Calendar create")
        if event_id in self._ambiguous_create_event_ids or self._ambiguous_create_after_success:
            self._ambiguous_create_after_success = False
            raise CalendarClientError(
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED,
                "Calendar write result requires reconciliation",
                retryable=True,
            )
        return snapshot

    async def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        owned_fields: CalendarEventPatch,
        if_match_etag: str,
    ) -> CalendarEventSnapshot:
        self.mutation_calls.append(("patch", calendar_id, event_id))
        event = self._events.get((calendar_id, event_id))
        if event is None:
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_EVENT_MISSING,
                "the Calendar event is missing",
            )
        if (
            event.lifecycle is not CalendarEventLifecycle.ACTIVE
            or event.interval is None
            or event.etag is None
        ):
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_STATE_CONFLICT,
                "the Calendar event is not active",
            )
        if event.etag != if_match_etag:
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_STATE_CONFLICT,
                "the Calendar event changed",
                retryable=True,
            )
        if self._patch_errors:
            raise self._patch_errors.pop(0)
        updated = CalendarEventSnapshot(
            calendar_id=calendar_id,
            event_id=event_id,
            interval=owned_fields.interval or event.interval,
            private_booking_id=owned_fields.private_booking_id or event.private_booking_id,
            etag=self._next_etag(),
        )
        self._events[(calendar_id, event_id)] = updated
        if self._crash_after_patch_success:
            self._crash_after_patch_success = False
            raise RuntimeError("simulated crash after Calendar patch")
        if self._ambiguous_patch_after_success:
            self._ambiguous_patch_after_success = False
            raise CalendarClientError(
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED,
                "Calendar write result requires reconciliation",
                retryable=True,
            )
        return updated

    async def cancel_event(self, calendar_id: str, event_id: str, if_match_etag: str) -> None:
        self.mutation_calls.append(("cancel", calendar_id, event_id))
        event = self._events.get((calendar_id, event_id))
        if event is None:
            return
        if (
            event.lifecycle is not CalendarEventLifecycle.ACTIVE
            or event.interval is None
            or event.etag is None
        ):
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_STATE_CONFLICT,
                "the Calendar event is not active",
            )
        if event.etag != if_match_etag:
            raise CalendarClientError(
                CalendarErrorCode.EXTERNAL_STATE_CONFLICT,
                "the Calendar event changed",
                retryable=True,
            )
        if self._cancel_errors:
            raise self._cancel_errors.pop(0)
        del self._events[(calendar_id, event_id)]
        if self._crash_after_cancel_success:
            self._crash_after_cancel_success = False
            raise RuntimeError("simulated crash after Calendar cancel")
        if self._ambiguous_cancel_after_success:
            self._ambiguous_cancel_after_success = False
            raise CalendarClientError(
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED,
                "Calendar write result requires reconciliation",
                retryable=True,
            )

"""Narrow Google Calendar boundary and provider-safe application models."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httplib2  # type: ignore[import-untyped]
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from googleapiclient.http import HttpRequest  # type: ignore[import-untyped]

from receptionist.core.config import Settings

_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105
_T = TypeVar("_T")


class CalendarErrorCode(StrEnum):
    CALENDAR_INTERVAL_UNAVAILABLE = "calendar_interval_unavailable"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    EXTERNAL_EVENT_MISSING = "external_event_missing"
    EXTERNAL_STATE_CONFLICT = "external_state_conflict"
    CALENDAR_RECONCILIATION_REQUIRED = "calendar_reconciliation_required"


class CalendarEventLifecycle(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class CalendarClientError(Exception):
    """A bounded integration error that does not expose provider details."""

    def __init__(self, code: CalendarErrorCode, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Calendar datetimes must be timezone-aware")
    if isinstance(value.tzinfo, ZoneInfo):
        wall_time = value.replace(tzinfo=None)
        valid_offsets = {
            candidate.utcoffset()
            for fold in (0, 1)
            if (candidate := value.replace(fold=fold))
            .astimezone(UTC)
            .astimezone(value.tzinfo)
            .replace(tzinfo=None)
            == wall_time
        }
        if not valid_offsets:
            raise ValueError("Calendar datetime is nonexistent")
        if len(valid_offsets) > 1:
            raise ValueError("Calendar datetime is ambiguous")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CalendarInterval:
    start_at_utc: datetime
    end_at_utc: datetime

    def __post_init__(self) -> None:
        start_at_utc = _utc_datetime(self.start_at_utc)
        end_at_utc = _utc_datetime(self.end_at_utc)
        if end_at_utc <= start_at_utc:
            raise ValueError("Calendar interval must have a positive duration")
        object.__setattr__(self, "start_at_utc", start_at_utc)
        object.__setattr__(self, "end_at_utc", end_at_utc)


@dataclass(frozen=True)
class CalendarBusyInterval:
    start_at_utc: datetime
    end_at_utc: datetime

    def __post_init__(self) -> None:
        interval = CalendarInterval(self.start_at_utc, self.end_at_utc)
        object.__setattr__(self, "start_at_utc", interval.start_at_utc)
        object.__setattr__(self, "end_at_utc", interval.end_at_utc)


@dataclass(frozen=True)
class CalendarConflict:
    event_id: str
    interval: CalendarInterval


@dataclass(frozen=True)
class CalendarEventSnapshot:
    calendar_id: str
    event_id: str
    interval: CalendarInterval | None
    private_booking_id: str | None
    etag: str | None
    lifecycle: CalendarEventLifecycle = CalendarEventLifecycle.ACTIVE


@dataclass(frozen=True)
class CalendarEventCreate:
    interval: CalendarInterval
    private_booking_id: str

    def __post_init__(self) -> None:
        if not self.private_booking_id.strip():
            raise ValueError("private booking marker must not be blank")


@dataclass(frozen=True)
class CalendarEventPatch:
    interval: CalendarInterval | None = None
    private_booking_id: str | None = None

    def __post_init__(self) -> None:
        if self.interval is None and self.private_booking_id is None:
            raise ValueError("Calendar patch must contain an owned field")
        if self.private_booking_id is not None and not self.private_booking_id.strip():
            raise ValueError("private booking marker must not be blank")


class CalendarClient(Protocol):
    @property
    def calendar_id(self) -> str: ...

    async def query_free_busy(
        self, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> tuple[CalendarBusyInterval, ...]: ...

    async def query_conflicts(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        exclude_event_id: str | None = None,
    ) -> tuple[CalendarConflict, ...]: ...

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventSnapshot | None: ...

    async def create_event(
        self, calendar_id: str, event_id: str, event: CalendarEventCreate
    ) -> CalendarEventSnapshot: ...

    async def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        owned_fields: CalendarEventPatch,
        if_match_etag: str,
    ) -> CalendarEventSnapshot: ...

    async def cancel_event(self, calendar_id: str, event_id: str, if_match_etag: str) -> None: ...


def _format_rfc3339(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _safe_error(
    code: CalendarErrorCode,
    *,
    retryable: bool = False,
) -> CalendarClientError:
    messages = {
        CalendarErrorCode.CALENDAR_INTERVAL_UNAVAILABLE: (
            "the requested Calendar interval is unavailable"
        ),
        CalendarErrorCode.CALENDAR_UNAVAILABLE: "the Calendar is unavailable",
        CalendarErrorCode.EXTERNAL_EVENT_MISSING: "the Calendar event is missing",
        CalendarErrorCode.EXTERNAL_STATE_CONFLICT: "the Calendar event changed",
        CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED: (
            "the Calendar write requires reconciliation"
        ),
    }
    return CalendarClientError(code, messages[code], retryable=retryable)


def _http_status(error: HttpError) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def _map_http_error(
    error: HttpError,
    *,
    missing_code: CalendarErrorCode | None,
    is_write: bool,
) -> CalendarClientError:
    status = _http_status(error)
    if status == 404 and missing_code is not None:
        return _safe_error(missing_code)
    if status == 412 and is_write:
        return _safe_error(CalendarErrorCode.EXTERNAL_STATE_CONFLICT, retryable=True)
    if status == 429:
        return _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE, retryable=True)
    if is_write and (status in {408, 409} or (status is not None and status >= 500)):
        return _safe_error(
            CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED,
            retryable=True,
        )
    retryable = status in {408, 429} or (status is not None and status >= 500)
    return _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE, retryable=retryable)


class GoogleCalendarClient:
    """One configured Google Calendar client with bounded async calls."""

    def __init__(self, settings: Settings, service: Any) -> None:
        self._timeout = settings.google_calendar_request_timeout_seconds
        if settings.google_calendar_id is None:
            raise ValueError("Google Calendar ID is required for the Calendar client")
        self._calendar_id = settings.google_calendar_id
        self._service = service

    @property
    def calendar_id(self) -> str:
        return self._calendar_id

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleCalendarClient:
        if not settings.google_calendar_configured:
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        client_id = settings.google_oauth_client_id
        client_secret = settings.google_oauth_client_secret
        refresh_token = settings.google_oauth_refresh_token
        if client_id is None or client_secret is None or refresh_token is None:
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        try:
            credentials = Credentials(
                token=None,
                refresh_token=refresh_token.get_secret_value(),
                token_uri=_TOKEN_URI,
                client_id=client_id.get_secret_value(),
                client_secret=client_secret.get_secret_value(),
                scopes=_OAUTH_SCOPES,  # type: ignore[no-untyped-call]
            )

            def request_builder(
                _service_http: Any,
                postproc: Any,
                uri: str,
                *,
                method: str = "GET",
                body: Any = None,
                headers: Any = None,
                methodId: str | None = None,
                resumable: Any = None,
            ) -> HttpRequest:
                transport = AuthorizedHttp(
                    credentials,
                    http=httplib2.Http(timeout=settings.google_calendar_request_timeout_seconds),
                )
                return HttpRequest(
                    transport,
                    postproc,
                    uri,
                    method=method,
                    body=body,
                    headers=headers,
                    methodId=methodId,
                    resumable=resumable,
                )

            service = build(
                "calendar",
                "v3",
                credentials=credentials,
                requestBuilder=request_builder,
                cache_discovery=False,
            )
        except Exception as exc:
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE) from exc
        return cls(settings, service)

    async def _execute(
        self,
        request_factory: Callable[[], Any],
        *,
        missing_code: CalendarErrorCode | None = None,
        is_write: bool = False,
    ) -> Any:
        worker = asyncio.create_task(
            asyncio.to_thread(lambda: request_factory().execute(num_retries=0))
        )
        try:
            if is_write:
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                    with suppress(BaseException):
                        worker.result()
                    raise
            return await asyncio.wait_for(worker, timeout=self._timeout)
        except TimeoutError as exc:
            code = (
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
                if is_write
                else CalendarErrorCode.CALENDAR_UNAVAILABLE
            )
            raise _safe_error(code, retryable=True) from exc
        except HttpError as exc:
            raise _map_http_error(exc, missing_code=missing_code, is_write=is_write) from exc
        except OSError as exc:
            code = (
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
                if is_write
                else CalendarErrorCode.CALENDAR_UNAVAILABLE
            )
            raise _safe_error(code, retryable=True) from exc
        except CalendarClientError:
            raise
        except Exception as exc:
            code = (
                CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
                if is_write
                else CalendarErrorCode.CALENDAR_UNAVAILABLE
            )
            raise _safe_error(code, retryable=is_write) from exc

    @staticmethod
    def _calendar_entry(response: Mapping[str, Any], calendar_id: str) -> Mapping[str, Any]:
        calendars = response.get("calendars")
        if not isinstance(calendars, Mapping):
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        entry = calendars.get(calendar_id)
        if not isinstance(entry, Mapping):
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        errors = entry.get("errors")
        if errors:
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        return entry

    async def query_free_busy(
        self, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> tuple[CalendarBusyInterval, ...]:
        requested = CalendarInterval(time_min, time_max)
        start = requested.start_at_utc
        end = requested.end_at_utc
        request_body = {
            "timeMin": _format_rfc3339(start),
            "timeMax": _format_rfc3339(end),
            "items": [{"id": calendar_id}],
        }
        response = await self._execute(lambda: self._service.freebusy().query(body=request_body))
        entry = self._calendar_entry(response, calendar_id)
        busy = entry.get("busy", [])
        if not isinstance(busy, list):
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        intervals: list[CalendarBusyInterval] = []
        for item in busy:
            if not isinstance(item, Mapping):
                raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
            intervals.append(
                CalendarBusyInterval(
                    _parse_provider_datetime(item.get("start")),
                    _parse_provider_datetime(item.get("end")),
                )
            )
        return tuple(intervals)

    async def query_conflicts(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        exclude_event_id: str | None = None,
    ) -> tuple[CalendarConflict, ...]:
        start = _utc_datetime(time_min)
        end = _utc_datetime(time_max)
        conflicts: list[CalendarConflict] = []
        page_token: str | None = None
        while True:
            params: dict[str, object] = {
                "calendarId": calendar_id,
                "timeMin": _format_rfc3339(start),
                "timeMax": _format_rfc3339(end),
                "singleEvents": True,
                "showDeleted": False,
            }
            if page_token is not None:
                params["pageToken"] = page_token

            def request_factory(params: dict[str, object] = params) -> Any:
                return self._service.events().list(**params)

            response = await self._execute(request_factory)
            if not isinstance(response, Mapping):
                raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
            response_timezone = response.get("timeZone")
            items = response.get("items", [])
            if not isinstance(items, list):
                raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
            for event in items:
                if not isinstance(event, Mapping):
                    raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
                if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                    continue
                event_id = event.get("id")
                if not isinstance(event_id, str):
                    raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
                if event_id == exclude_event_id:
                    continue
                interval = _event_interval(event, response_timezone)
                if interval.start_at_utc < end and start < interval.end_at_utc:
                    conflicts.append(CalendarConflict(event_id=event_id, interval=interval))
            next_page_token = response.get("nextPageToken")
            if not isinstance(next_page_token, str) or not next_page_token:
                break
            page_token = next_page_token
        return tuple(conflicts)

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEventSnapshot | None:
        try:
            response = await self._execute(
                lambda: self._service.events().get(calendarId=calendar_id, eventId=event_id),
                missing_code=CalendarErrorCode.EXTERNAL_EVENT_MISSING,
            )
        except CalendarClientError as exc:
            if exc.code is CalendarErrorCode.EXTERNAL_EVENT_MISSING:
                return None
            raise
        return _event_snapshot(response, calendar_id)

    async def create_event(
        self, calendar_id: str, event_id: str, event: CalendarEventCreate
    ) -> CalendarEventSnapshot:
        body = {
            "id": event_id,
            "start": _event_time(event.interval.start_at_utc),
            "end": _event_time(event.interval.end_at_utc),
            "extendedProperties": {
                "private": {"receptionist_booking_id": event.private_booking_id}
            },
        }
        response = await self._execute(
            lambda: self._service.events().insert(
                calendarId=calendar_id,
                body=body,
            ),
            is_write=True,
        )
        return _event_snapshot(response, calendar_id)

    async def patch_event(
        self,
        calendar_id: str,
        event_id: str,
        owned_fields: CalendarEventPatch,
        if_match_etag: str,
    ) -> CalendarEventSnapshot:
        body: dict[str, Any] = {}
        if owned_fields.interval is not None:
            body["start"] = _event_time(owned_fields.interval.start_at_utc)
            body["end"] = _event_time(owned_fields.interval.end_at_utc)
        if owned_fields.private_booking_id is not None:
            body["extendedProperties"] = {
                "private": {"receptionist_booking_id": owned_fields.private_booking_id}
            }

        def request_factory() -> Any:
            request = self._service.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
            )
            request.headers["If-Match"] = if_match_etag
            return request

        response = await self._execute(
            request_factory,
            missing_code=CalendarErrorCode.EXTERNAL_EVENT_MISSING,
            is_write=True,
        )
        return _event_snapshot(response, calendar_id)

    async def cancel_event(self, calendar_id: str, event_id: str, if_match_etag: str) -> None:
        def request_factory() -> Any:
            request = self._service.events().delete(calendarId=calendar_id, eventId=event_id)
            request.headers["If-Match"] = if_match_etag
            return request

        await self._execute(
            request_factory,
            missing_code=CalendarErrorCode.EXTERNAL_EVENT_MISSING,
            is_write=True,
        )


def _parse_provider_datetime(value: object, timezone_name: object = None) -> datetime:
    if not isinstance(value, str):
        raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if not isinstance(timezone_name, str):
                raise ValueError("missing provider timezone")
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return _utc_datetime(parsed)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE) from exc


def _event_interval(event: Mapping[str, Any], response_timezone: object) -> CalendarInterval:
    start = event.get("start")
    end = event.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
    start_timezone = start.get("timeZone") or end.get("timeZone") or response_timezone
    if "dateTime" in start or "dateTime" in end:
        start_at_utc = _parse_provider_datetime(start.get("dateTime"), start_timezone)
        end_at_utc = _parse_provider_datetime(end.get("dateTime"), start_timezone)
    elif "date" in start or "date" in end:
        if not isinstance(start_timezone, str):
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
        try:
            zone = ZoneInfo(start_timezone)
            start_date = date.fromisoformat(str(start["date"]))
            end_date = date.fromisoformat(str(end["date"]))
            start_at_utc = _utc_datetime(datetime.combine(start_date, time.min, tzinfo=zone))
            end_at_utc = _utc_datetime(datetime.combine(end_date, time.min, tzinfo=zone))
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE) from exc
    else:
        raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE)
    try:
        return CalendarInterval(start_at_utc, end_at_utc)
    except ValueError as exc:
        raise _safe_error(CalendarErrorCode.CALENDAR_UNAVAILABLE) from exc


def _event_snapshot(event: Mapping[str, Any], calendar_id: str) -> CalendarEventSnapshot:
    event_id = event.get("id")
    if not isinstance(event_id, str):
        raise _safe_error(CalendarErrorCode.EXTERNAL_STATE_CONFLICT)
    private_booking_id: str | None = None
    extended_properties = event.get("extendedProperties")
    if isinstance(extended_properties, Mapping):
        private_properties = extended_properties.get("private")
        if isinstance(private_properties, Mapping):
            marker = private_properties.get("receptionist_booking_id")
            if isinstance(marker, str):
                private_booking_id = marker
    if event.get("status") == CalendarEventLifecycle.CANCELLED:
        etag = event.get("etag")
        return CalendarEventSnapshot(
            calendar_id=calendar_id,
            event_id=event_id,
            interval=None,
            private_booking_id=private_booking_id,
            etag=etag if isinstance(etag, str) else None,
            lifecycle=CalendarEventLifecycle.CANCELLED,
        )
    etag = event.get("etag")
    if not isinstance(etag, str):
        raise _safe_error(CalendarErrorCode.EXTERNAL_STATE_CONFLICT)
    interval = _event_interval(event, event.get("timeZone"))
    return CalendarEventSnapshot(
        calendar_id=calendar_id,
        event_id=event_id,
        interval=interval,
        private_booking_id=private_booking_id,
        etag=etag,
        lifecycle=CalendarEventLifecycle.ACTIVE,
    )


def _event_time(value: datetime) -> dict[str, str]:
    return {"dateTime": _format_rfc3339(value), "timeZone": "UTC"}

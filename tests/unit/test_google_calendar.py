from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, call

import pytest
from googleapiclient.errors import HttpError
from pydantic import SecretStr

from receptionist.core.config import Settings
from receptionist.integrations import google_calendar
from receptionist.integrations.google_calendar import (
    CalendarClientError,
    CalendarErrorCode,
    CalendarEventCreate,
    CalendarEventPatch,
    CalendarInterval,
    GoogleCalendarClient,
)

pytestmark = pytest.mark.unit

CALENDAR_ID = "calendar-id"
TIME_MIN = datetime(2026, 10, 11, 7, tzinfo=UTC)
TIME_MAX = datetime(2026, 10, 11, 9, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url="postgresql+psycopg://receptionist:test@localhost:55432/receptionist_test",
        google_calendar_id=CALENDAR_ID,
        google_oauth_client_id="client-id",
        google_oauth_client_secret=SecretStr("test-client-placeholder"),
        google_oauth_refresh_token=SecretStr("test-refresh-placeholder"),
        google_calendar_request_timeout_seconds=7.5,
    )


class _Request:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.headers: dict[str, str] = {}
        self.execute_calls: list[dict[str, object]] = []

    def execute(self, **kwargs: object) -> dict[str, object]:
        self.execute_calls.append(kwargs)
        return self.response


class _ErrorRequest:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.headers: dict[str, str] = {}

    def execute(self, **kwargs: object) -> dict[str, object]:
        raise self.error


class _FreeBusyResource:
    def __init__(self, response: dict[str, object]) -> None:
        self.request = _Request(response)
        self.kwargs: dict[str, object] | None = None

    def query(self, **kwargs: object) -> _Request:
        self.kwargs = kwargs
        return self.request


class _EventsResource:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.list_calls: list[dict[str, object]] = []
        self.get_request = _Request({})
        self.insert_request = _Request({})
        self.patch_request = _Request({})
        self.delete_request = _Request({})

    def list(self, **kwargs: object) -> _Request:
        self.list_calls.append(kwargs)
        page_index = 0 if kwargs.get("pageToken") is None else 1
        self.list_request = _Request(self.pages[page_index])
        return self.list_request

    def get(self, **kwargs: object) -> _Request:
        self.get_kwargs = kwargs
        return self.get_request

    def insert(self, *, calendarId: object, body: object) -> _Request:
        self.insert_kwargs = {"calendarId": calendarId, "body": body}
        return self.insert_request

    def patch(self, **kwargs: object) -> _Request:
        self.patch_kwargs = kwargs
        return self.patch_request

    def delete(self, **kwargs: object) -> _Request:
        self.delete_kwargs = kwargs
        return self.delete_request


class _Service:
    def __init__(self, free_busy: _FreeBusyResource, events: _EventsResource) -> None:
        self.free_busy = free_busy
        self.events_resource = events

    def freebusy(self) -> _FreeBusyResource:
        return self.free_busy

    def events(self) -> _EventsResource:
        return self.events_resource


def _client(service: _Service) -> GoogleCalendarClient:
    return GoogleCalendarClient(_settings(), service)


def test_google_calendar_client_exposes_configured_calendar_id() -> None:
    client = _client(_Service(_FreeBusyResource({}), _EventsResource([{}])))

    assert client.calendar_id == CALENDAR_ID


def test_google_credentials_use_exactly_the_two_approved_scopes(monkeypatch) -> None:
    credentials = Mock(return_value=Mock())
    authorized_one = Mock(name="authorized-1")
    authorized_two = Mock(name="authorized-2")
    transport_one = Mock(name="transport-1")
    transport_two = Mock(name="transport-2")
    authorized_http = Mock(side_effect=[authorized_one, authorized_two])
    http = Mock(side_effect=[transport_one, transport_two])
    build = Mock(return_value=Mock())
    monkeypatch.setattr(google_calendar, "Credentials", credentials)
    monkeypatch.setattr(google_calendar, "AuthorizedHttp", authorized_http)
    monkeypatch.setattr(google_calendar.httplib2, "Http", http)
    monkeypatch.setattr(google_calendar, "build", build)

    GoogleCalendarClient.from_settings(_settings())

    scopes = credentials.call_args.kwargs["scopes"]
    assert scopes == (
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    )
    assert "https://www.googleapis.com/auth/calendar" not in scopes
    build.assert_called_once()
    build_kwargs = build.call_args.kwargs
    assert "http" not in build_kwargs
    assert build_kwargs["credentials"] is credentials.return_value
    assert build_kwargs["cache_discovery"] is False

    request_builder = build_kwargs["requestBuilder"]
    request_one = request_builder(
        Mock(), Mock(), "https://calendar.test/one", method="GET", body=None, headers={}
    )
    request_two = request_builder(
        Mock(), Mock(), "https://calendar.test/two", method="GET", body=None, headers={}
    )

    assert request_one.http is authorized_one
    assert request_two.http is authorized_two
    assert request_one.http is not request_two.http
    http.assert_has_calls([call(timeout=7.5), call(timeout=7.5)])
    authorized_http.assert_has_calls(
        [
            call(credentials.return_value, http=transport_one),
            call(credentials.return_value, http=transport_two),
        ]
    )
    assert transport_one is not transport_two


def test_google_client_setup_errors_do_not_leak_credentials(monkeypatch, caplog) -> None:
    credentials = Mock(return_value=Mock())
    build = Mock(
        side_effect=RuntimeError(
            "provider setup failed with test-client-placeholder and test-refresh-placeholder"
        )
    )
    monkeypatch.setattr(google_calendar, "Credentials", credentials)
    monkeypatch.setattr(google_calendar, "build", build)

    with pytest.raises(CalendarClientError) as exc_info:
        GoogleCalendarClient.from_settings(_settings())

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE
    assert "test-client-placeholder" not in str(exc_info.value)
    assert "test-refresh-placeholder" not in str(exc_info.value)
    assert "test-client-placeholder" not in caplog.text
    assert "test-refresh-placeholder" not in caplog.text


async def test_provider_failures_map_to_bounded_errors_without_raw_details() -> None:
    request = _ErrorRequest(HttpError(Mock(status=401), b"provider-body"))
    events = _EventsResource([{}])
    events.get_request = request  # type: ignore[assignment]
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.get_event(CALENDAR_ID, "event-id")

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE
    assert not exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        (404, None, False),
        (412, CalendarErrorCode.CALENDAR_UNAVAILABLE, False),
        (429, CalendarErrorCode.CALENDAR_UNAVAILABLE, True),
        (500, CalendarErrorCode.CALENDAR_UNAVAILABLE, True),
    ],
)
async def test_http_status_mapping_is_bounded(
    status: int,
    expected_code: CalendarErrorCode | None,
    retryable: bool,
) -> None:
    events = _EventsResource([{}])
    events.get_request = _ErrorRequest(HttpError(Mock(status=status), b"provider-body"))  # type: ignore[assignment]
    client = _client(_Service(_FreeBusyResource({}), events))

    if status == 404:
        assert await client.get_event(CALENDAR_ID, "event-id") is None
        return

    with pytest.raises(CalendarClientError) as exc_info:
        await client.get_event(CALENDAR_ID, "event-id")
    assert exc_info.value.code is expected_code
    assert exc_info.value.retryable is retryable
    assert "provider-body" not in str(exc_info.value)


async def test_request_timeout_fails_closed(monkeypatch) -> None:
    async def timeout_to_thread(function, *args, **kwargs):
        raise TimeoutError("provider-body")

    monkeypatch.setattr(google_calendar.asyncio, "to_thread", timeout_to_thread)
    client = _client(_Service(_FreeBusyResource({}), _EventsResource([{}])))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.get_event(CALENDAR_ID, "event-id")
    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["create", "patch", "cancel"])
async def test_write_timeout_requires_reconciliation(monkeypatch, operation: str) -> None:
    async def timeout_to_thread(function, *args, **kwargs):
        raise TimeoutError("provider-body")

    monkeypatch.setattr(google_calendar.asyncio, "to_thread", timeout_to_thread)
    client = _client(_Service(_FreeBusyResource({}), _EventsResource([{}])))

    with pytest.raises(CalendarClientError) as exc_info:
        if operation == "create":
            await client.create_event(
                CALENDAR_ID,
                "event-id",
                CalendarEventCreate(
                    interval=CalendarInterval(TIME_MIN, TIME_MAX),
                    private_booking_id="booking-id",
                ),
            )
        elif operation == "patch":
            await client.patch_event(
                CALENDAR_ID,
                "event-id",
                CalendarEventPatch(interval=CalendarInterval(TIME_MIN, TIME_MAX)),
                "etag-1",
            )
        else:
            await client.cancel_event(CALENDAR_ID, "event-id", "etag-1")

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


async def test_create_transport_failure_requires_reconciliation() -> None:
    events = _EventsResource([{}])
    events.insert_request = _ErrorRequest(OSError("provider-body"))
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.create_event(
            CALENDAR_ID,
            "event-id",
            CalendarEventCreate(
                interval=CalendarInterval(TIME_MIN, TIME_MAX),
                private_booking_id="booking-id",
            ),
        )

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize("status", [408, 409, 500])
async def test_create_uncertain_http_failure_requires_reconciliation(status: int) -> None:
    events = _EventsResource([{}])
    events.insert_request = _ErrorRequest(HttpError(Mock(status=status), b"provider-body"))
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.create_event(
            CALENDAR_ID,
            "event-id",
            CalendarEventCreate(
                interval=CalendarInterval(TIME_MIN, TIME_MAX),
                private_booking_id="booking-id",
            ),
        )

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["patch", "cancel"])
async def test_patch_and_delete_5xx_require_reconciliation(operation: str) -> None:
    events = _EventsResource([{}])
    request = _ErrorRequest(HttpError(Mock(status=500), b"provider-body"))
    if operation == "patch":
        events.patch_request = request
    else:
        events.delete_request = request
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        if operation == "patch":
            await client.patch_event(
                CALENDAR_ID,
                "event-id",
                CalendarEventPatch(interval=CalendarInterval(TIME_MIN, TIME_MAX)),
                "etag-1",
            )
        else:
            await client.cancel_event(CALENDAR_ID, "event-id", "etag-1")

    assert exc_info.value.code is CalendarErrorCode.CALENDAR_RECONCILIATION_REQUIRED
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["patch", "cancel"])
async def test_patch_and_delete_412_remain_external_state_conflicts(operation: str) -> None:
    events = _EventsResource([{}])
    request = _ErrorRequest(HttpError(Mock(status=412), b"provider-body"))
    if operation == "patch":
        events.patch_request = request
    else:
        events.delete_request = request
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        if operation == "patch":
            await client.patch_event(
                CALENDAR_ID,
                "event-id",
                CalendarEventPatch(interval=CalendarInterval(TIME_MIN, TIME_MAX)),
                "etag-1",
            )
        else:
            await client.cancel_event(CALENDAR_ID, "event-id", "etag-1")

    assert exc_info.value.code is CalendarErrorCode.EXTERNAL_STATE_CONFLICT
    assert exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize("operation", ["patch", "cancel"])
async def test_patch_and_delete_404_map_to_missing_event(operation: str) -> None:
    events = _EventsResource([{}])
    request = _ErrorRequest(HttpError(Mock(status=404), b"provider-body"))
    if operation == "patch":
        events.patch_request = request
    else:
        events.delete_request = request
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        if operation == "patch":
            await client.patch_event(
                CALENDAR_ID,
                "event-id",
                CalendarEventPatch(interval=CalendarInterval(TIME_MIN, TIME_MAX)),
                "etag-1",
            )
        else:
            await client.cancel_event(CALENDAR_ID, "event-id", "etag-1")

    assert exc_info.value.code is CalendarErrorCode.EXTERNAL_EVENT_MISSING
    assert not exc_info.value.retryable
    assert "provider-body" not in str(exc_info.value)


@pytest.mark.parametrize(
    "calendar_error",
    ["notFound", "internalError", "unknownFutureReason"],
)
async def test_free_busy_calendar_errors_fail_closed_without_provider_details(
    calendar_error: str,
) -> None:
    free_busy = _FreeBusyResource(
        {
            "calendars": {
                CALENDAR_ID: {
                    "errors": [{"reason": calendar_error, "detail": "provider-body"}],
                    "busy": [],
                }
            }
        }
    )
    client = _client(_Service(free_busy, _EventsResource([{}])))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.query_free_busy(CALENDAR_ID, TIME_MIN, TIME_MAX)
    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE
    assert "provider-body" not in str(exc_info.value)
    assert calendar_error not in str(exc_info.value)


async def test_free_busy_translation_uses_calendar_id_and_utc_bounds() -> None:
    free_busy = _FreeBusyResource(
        {
            "calendars": {
                CALENDAR_ID: {
                    "busy": [{"start": "2026-10-11T08:00:00Z", "end": "2026-10-11T08:30:00Z"}]
                }
            }
        }
    )
    client = _client(_Service(free_busy, _EventsResource([{}])))

    busy = await client.query_free_busy(CALENDAR_ID, TIME_MIN, TIME_MAX)

    assert busy[0].start_at_utc == datetime(2026, 10, 11, 8, tzinfo=UTC)
    assert free_busy.kwargs == {
        "body": {
            "timeMin": "2026-10-11T07:00:00Z",
            "timeMax": "2026-10-11T09:00:00Z",
            "items": [{"id": CALENDAR_ID}],
        }
    }


async def test_conflicts_follow_all_pages_repeat_bounds_and_exclude_exact_event_only() -> None:
    events = _EventsResource(
        [
            {
                "timeZone": "Asia/Riyadh",
                "nextPageToken": "page-2",
                "items": [
                    {
                        "id": "target-event",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-10-11T10:00:00+03:00"},
                        "end": {"dateTime": "2026-10-11T11:00:00+03:00"},
                    },
                    {
                        "id": "touching-event",
                        "status": "confirmed",
                        "start": {"dateTime": "2026-10-11T04:00:00Z"},
                        "end": {"dateTime": "2026-10-11T07:00:00Z"},
                    },
                ],
            },
            {
                "timeZone": "Asia/Riyadh",
                "items": [
                    {
                        "id": "later-blocker",
                        "status": "confirmed",
                        "recurringEventId": "series-id",
                        "start": {"dateTime": "2026-10-11T08:00:00Z"},
                        "end": {"dateTime": "2026-10-11T08:30:00Z"},
                    },
                    {
                        "id": "opaque-all-day",
                        "status": "confirmed",
                        "transparency": "opaque",
                        "start": {"date": "2026-10-11"},
                        "end": {"date": "2026-10-12"},
                    },
                    {
                        "id": "transparent-all-day",
                        "status": "confirmed",
                        "transparency": "transparent",
                        "start": {"date": "2026-10-11"},
                        "end": {"date": "2026-10-12"},
                    },
                ],
            },
        ]
    )
    client = _client(_Service(_FreeBusyResource({}), events))

    conflicts = await client.query_conflicts(
        CALENDAR_ID,
        datetime(2026, 10, 11, 7, tzinfo=UTC),
        datetime(2026, 10, 11, 9, tzinfo=UTC),
        exclude_event_id="target-event",
    )

    assert {conflict.event_id for conflict in conflicts} == {"later-blocker", "opaque-all-day"}
    assert len(events.list_calls) == 2
    for list_call in events.list_calls:
        assert list_call["calendarId"] == CALENDAR_ID
        assert list_call["timeMin"] == "2026-10-11T07:00:00Z"
        assert list_call["timeMax"] == "2026-10-11T09:00:00Z"
        assert list_call["singleEvents"] is True
        assert list_call["showDeleted"] is False


async def test_all_day_event_requires_safe_timezone_translation() -> None:
    events = _EventsResource(
        [
            {
                "items": [
                    {
                        "id": "all-day",
                        "start": {"date": "2026-10-11"},
                        "end": {"date": "2026-10-12"},
                    }
                ]
            }
        ]
    )
    client = _client(_Service(_FreeBusyResource({}), events))

    with pytest.raises(CalendarClientError) as exc_info:
        await client.query_conflicts(CALENDAR_ID, TIME_MIN, TIME_MAX)
    assert exc_info.value.code is CalendarErrorCode.CALENDAR_UNAVAILABLE


async def test_mutation_translation_uses_caller_event_id_private_marker_and_owned_patch() -> None:
    events = _EventsResource([{}])
    events.insert_request.response = {
        "id": "event-id",
        "etag": "etag-1",
        "start": {"dateTime": "2026-10-11T07:00:00Z"},
        "end": {"dateTime": "2026-10-11T08:00:00Z"},
        "extendedProperties": {"private": {"receptionist_booking_id": "booking-id"}},
    }
    events.patch_request.response = events.insert_request.response
    client = _client(_Service(_FreeBusyResource({}), events))
    create = CalendarEventCreate(
        interval=CalendarInterval(TIME_MIN, TIME_MAX), private_booking_id="booking-id"
    )

    await client.create_event(CALENDAR_ID, "event-id", create)
    assert events.insert_kwargs["calendarId"] == CALENDAR_ID
    assert events.insert_kwargs["body"]["id"] == "event-id"
    assert "eventId" not in events.insert_kwargs
    assert events.insert_kwargs["body"]["extendedProperties"]["private"] == {
        "receptionist_booking_id": "booking-id"
    }

    patch = CalendarEventPatch(interval=CalendarInterval(TIME_MIN, TIME_MAX))
    await client.patch_event(CALENDAR_ID, "event-id", patch, "etag-1")
    assert events.patch_kwargs["body"] == {
        "start": {"dateTime": "2026-10-11T07:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-11T09:00:00Z", "timeZone": "UTC"},
    }
    assert events.patch_request.headers["If-Match"] == "etag-1"
    assert "description" not in events.patch_kwargs["body"]

    await client.cancel_event(CALENDAR_ID, "event-id", "etag-1")
    assert events.delete_request.headers["If-Match"] == "etag-1"

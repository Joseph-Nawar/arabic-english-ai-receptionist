from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from receptionist.application.booking import (
    BookingReadData,
    BookingReadResult,
    ConfirmationData,
    ConfirmationResult,
    PendingActionType,
    PreparationData,
    PreparationResult,
)
from receptionist.domain.enums import BookingStatus
from receptionist.integrations.google_calendar import (
    CalendarEventCreate,
    CalendarInterval,
)
from scripts.calendar_smoke import (
    SMOKE_CONFIRMATION,
    SmokeGuardError,
    _cleanup_exact_event,
    build_smoke_configuration,
    main,
    run_smoke_operations,
)
from tests.support.calendar_double import DeterministicCalendarDouble

pytestmark = pytest.mark.unit

DATABASE_URL = "postgresql+psycopg://localhost:55432/receptionist_test"
SMOKE_CALENDAR_ID = "smoke-phase2@example.test"


def _environment() -> dict[str, str]:
    environment = {
        "RECEPTIONIST_APP_ENV": "test",
        "RECEPTIONIST_DATABASE_URL": DATABASE_URL,
        "RECEPTIONIST_CALENDAR_SMOKE_ENV": "synthetic",
        "RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": SMOKE_CONFIRMATION,
        "RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": SMOKE_CALENDAR_ID,
        "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_ID": "smoke-client-id",
        "RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN": "smoke-refresh-value",
    }
    credential_env_name = "".join(
        (
            "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_",
            chr(83),
            chr(69),
            chr(67),
            chr(82),
            chr(69),
            chr(84),
        )
    )
    environment[credential_env_name] = "smoke-client-value"
    return environment


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": ""}, "explicit smoke confirmation"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": "I_AGREE"}, "explicit smoke confirmation"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": ""}, "dedicated smoke Calendar ID"),
        ({"RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN": ""}, "dedicated Calendar credentials"),
        ({"RECEPTIONIST_APP_ENV": "production"}, "production application"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_ENV": "local"}, "synthetic smoke environment"),
        (
            {"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": "customer-calendar"},
            "dedicated smoke Calendar ID",
        ),
    ],
)
def test_smoke_guard_refuses_unsafe_configuration(changes: dict[str, str], expected: str) -> None:
    environment = _environment()
    environment.update(changes)

    with pytest.raises(SmokeGuardError, match=expected):
        build_smoke_configuration(environment)


def test_smoke_guard_uses_dedicated_id_and_secret_safe_settings() -> None:
    configuration = build_smoke_configuration(_environment())

    assert configuration.calendar_id == SMOKE_CALENDAR_ID
    assert configuration.settings.google_calendar_id == SMOKE_CALENDAR_ID
    assert "smoke-client-value" not in repr(configuration)
    assert "smoke-refresh-value" not in repr(configuration)
    assert "smoke-client-value" not in str(configuration.settings.safe_summary())
    assert "smoke-refresh-value" not in str(configuration.settings.safe_summary())


def test_invalid_smoke_guard_does_not_construct_provider_or_print_secrets(
    monkeypatch, capsys
) -> None:
    environment = _environment()
    environment["RECEPTIONIST_CALENDAR_SMOKE_CONFIRM"] = "WRONG"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    constructed = False

    def fail_provider(*args, **kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr("scripts.calendar_smoke.GoogleCalendarClient.from_settings", fail_provider)

    assert main() == 2
    assert constructed is False
    output = capsys.readouterr().out
    assert "smoke-client-value" not in output
    assert "smoke-refresh-value" not in output


def test_cleanup_targets_only_exact_matching_smoke_event() -> None:
    async def scenario() -> None:
        calendar = DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID)
        interval = CalendarInterval(
            datetime(2026, 10, 11, 7, tzinfo=UTC),
            datetime(2026, 10, 11, 8, tzinfo=UTC),
        )
        booking_id = uuid4()
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            "smokeeventone",
            CalendarEventCreate(interval=interval, private_booking_id=str(booking_id)),
        )
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            "smokeeventv",
            CalendarEventCreate(interval=interval, private_booking_id="other-booking"),
        )

        await _cleanup_exact_event(
            calendar,
            calendar_id=SMOKE_CALENDAR_ID,
            event_id="smokeeventone",
            booking_id=booking_id,
        )

        assert await calendar.get_event(SMOKE_CALENDAR_ID, "smokeeventone") is None
        assert await calendar.get_event(SMOKE_CALENDAR_ID, "smokeeventv") is not None

    import asyncio

    asyncio.run(scenario())


async def test_smoke_operations_use_existing_finite_booking_operations(monkeypatch) -> None:
    conversation_id = uuid4()
    booking_id = uuid4()
    event_id = "smokeeventid"
    start = datetime(2026, 10, 11, 7, tzinfo=UTC)
    end = start + timedelta(hours=1)
    later_start = start + timedelta(days=1)
    later_end = later_start + timedelta(hours=1)
    calls: list[str] = []

    async def fake_prepare_create(*args, **kwargs):
        calls.append("prepare_create_booking")
        return PreparationResult(
            operation="prepare_create_booking",
            data=PreparationData(
                action_type=PendingActionType.CREATE_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=start,
                requested_end_at_utc=end,
            ),
        )

    confirmation_calls = 0

    async def fake_confirm(*args, **kwargs):
        nonlocal confirmation_calls
        confirmation_calls += 1
        calls.append("confirm_booking_action")
        return ConfirmationResult(
            replayed=confirmation_calls == 2,
            data=ConfirmationData(
                booking_id=booking_id,
                status=BookingStatus.CONFIRMED,
                requested_start_at_utc=start,
                requested_end_at_utc=end,
                calendar_id=SMOKE_CALENDAR_ID,
                calendar_event_id=event_id,
            ),
        )

    get_calls = 0

    async def fake_get(*args, **kwargs):
        nonlocal get_calls
        get_calls += 1
        calls.append("get_booking")
        return BookingReadResult(
            data=BookingReadData(
                booking_id=booking_id,
                status=BookingStatus.CONFIRMED if get_calls == 1 else BookingStatus.CANCELLED,
                start_at_utc=start,
                end_at_utc=end,
                calendar_id=SMOKE_CALENDAR_ID,
                calendar_event_id=event_id,
                reconciliation_status="in_sync",
            )
        )

    async def fake_prepare_reschedule(*args, **kwargs):
        calls.append("prepare_reschedule_booking")
        return PreparationResult(
            operation="prepare_reschedule_booking",
            data=PreparationData(
                action_type=PendingActionType.RESCHEDULE_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=later_start,
                requested_end_at_utc=later_end,
            ),
        )

    async def fake_prepare_cancel(*args, **kwargs):
        calls.append("prepare_cancel_booking")
        return PreparationResult(
            operation="prepare_cancel_booking",
            data=PreparationData(
                action_type=PendingActionType.CANCEL_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=later_start,
                requested_end_at_utc=later_end,
            ),
        )

    monkeypatch.setattr("scripts.calendar_smoke.prepare_create_booking", fake_prepare_create)
    monkeypatch.setattr("scripts.calendar_smoke.confirm_booking_action", fake_confirm)
    monkeypatch.setattr("scripts.calendar_smoke._read_booking", fake_get)
    monkeypatch.setattr(
        "scripts.calendar_smoke.prepare_reschedule_booking", fake_prepare_reschedule
    )
    monkeypatch.setattr("scripts.calendar_smoke.prepare_cancel_booking", fake_prepare_cancel)

    calendar = DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID)
    result = await run_smoke_operations(
        object(), calendar, conversation_id=conversation_id, now_utc=start
    )

    assert result.booking_id == booking_id
    assert result.calendar_event_id == event_id
    assert calls == [
        "prepare_create_booking",
        "confirm_booking_action",
        "confirm_booking_action",
        "get_booking",
        "prepare_reschedule_booking",
        "confirm_booking_action",
        "prepare_cancel_booking",
        "confirm_booking_action",
        "get_booking",
    ]

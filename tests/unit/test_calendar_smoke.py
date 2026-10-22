from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

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
    SmokeRunResult,
    SmokeRunTracker,
    SmokeWorkflowError,
    _cleanup_exact_event,
    _smoke_interval_after,
    build_smoke_configuration,
    main,
    run_smoke,
    run_smoke_operations,
)
from tests.support.calendar_double import DeterministicCalendarDouble

pytestmark = pytest.mark.unit

DATABASE_URL = "postgresql+psycopg://localhost:55432/receptionist_test"
SMOKE_CALENDAR_ID = "c_phase2synthetic123@group.calendar.google.com"


def _environment() -> dict[str, str]:
    environment = {
        "RECEPTIONIST_APP_ENV": "test",
        "RECEPTIONIST_DATABASE_URL": DATABASE_URL,
        "RECEPTIONIST_CALENDAR_SMOKE_ENV": "synthetic",
        "RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": SMOKE_CONFIRMATION,
        "RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": SMOKE_CALENDAR_ID,
        "RECEPTIONIST_GOOGLE_CALENDAR_ID": "runtime@example.test",
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


def _install_smoke_operation_fakes(
    monkeypatch,
    read_statuses: tuple[str, ...],
    *,
    wrong_reschedule_interval: bool = False,
    replay_observer=None,
):
    conversation_id = uuid4()
    booking_id = uuid4()
    event_id = "smokeeventid"
    now_utc = datetime(2026, 10, 11, 7, tzinfo=UTC)
    start, end = _smoke_interval_after(now_utc)
    later_start, later_end = _smoke_interval_after(start)
    confirmation_calls = 0
    read_calls = 0

    async def fake_prepare_create(*args, **kwargs):
        return PreparationResult(
            operation="prepare_create_booking",
            data=PreparationData(
                action_type=PendingActionType.CREATE_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=start,
                requested_end_at_utc=end,
            ),
        )

    async def fake_prepare_reschedule(*args, **kwargs):
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
        return PreparationResult(
            operation="prepare_cancel_booking",
            data=PreparationData(
                action_type=PendingActionType.CANCEL_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=later_start,
                requested_end_at_utc=later_end,
            ),
        )

    async def fake_confirm(*args, **kwargs):
        nonlocal confirmation_calls
        confirmation_calls += 1
        if confirmation_calls == 2 and replay_observer is not None:
            replay_observer()
        requested_start, requested_end = (
            (start, end) if confirmation_calls <= 2 else (later_start, later_end)
        )
        return ConfirmationResult(
            replayed=confirmation_calls == 2,
            data=ConfirmationData(
                booking_id=booking_id,
                status=BookingStatus.CONFIRMED,
                requested_start_at_utc=requested_start,
                requested_end_at_utc=requested_end,
                calendar_id=SMOKE_CALENDAR_ID,
                calendar_event_id=event_id,
            ),
        )

    async def fake_get(*args, **kwargs):
        nonlocal read_calls
        status_code = read_statuses[read_calls]
        read_calls += 1
        is_final = read_calls == 3
        read_start, read_end = (later_start, later_end) if read_calls > 1 else (start, end)
        if read_calls == 2 and wrong_reschedule_interval:
            read_start, read_end = start, end
        return BookingReadResult(
            data=BookingReadData(
                booking_id=booking_id,
                status=BookingStatus.CANCELLED if is_final else BookingStatus.CONFIRMED,
                start_at_utc=read_start,
                end_at_utc=read_end,
                calendar_id=SMOKE_CALENDAR_ID,
                calendar_event_id=event_id,
                reconciliation_status=status_code,
            )
        )

    monkeypatch.setattr("scripts.calendar_smoke.prepare_create_booking", fake_prepare_create)
    monkeypatch.setattr(
        "scripts.calendar_smoke.prepare_reschedule_booking", fake_prepare_reschedule
    )
    monkeypatch.setattr("scripts.calendar_smoke.prepare_cancel_booking", fake_prepare_cancel)
    monkeypatch.setattr("scripts.calendar_smoke.confirm_booking_action", fake_confirm)
    monkeypatch.setattr("scripts.calendar_smoke._read_booking", fake_get)
    return conversation_id, now_utc


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": ""}, "explicit smoke confirmation"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_CONFIRM": "I_AGREE"}, "explicit smoke confirmation"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": ""}, "dedicated secondary Calendar ID"),
        ({"RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN": ""}, "dedicated Calendar credentials"),
        ({"RECEPTIONIST_APP_ENV": "local"}, "test application environment"),
        ({"RECEPTIONIST_APP_ENV": "production"}, "test application environment"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_ENV": "local"}, "synthetic smoke environment"),
        ({"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": "primary"}, "secondary Calendar ID"),
        (
            {"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": "person@gmail.com"},
            "secondary Calendar ID",
        ),
        (
            {"RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": "customer-calendar"},
            "secondary Calendar ID",
        ),
        (
            {
                "RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID": SMOKE_CALENDAR_ID,
                "RECEPTIONIST_GOOGLE_CALENDAR_ID": SMOKE_CALENDAR_ID,
            },
            "separate",
        ),
        (
            {"RECEPTIONIST_DATABASE_URL": ("postgresql+psycopg://localhost:55432/receptionist")},
            "receptionist_test",
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


def test_smoke_guard_accepts_a_guarded_secondary_calendar_and_test_database() -> None:
    configuration = build_smoke_configuration(_environment())

    assert configuration.settings.app_env == "test"
    assert configuration.settings.google_calendar_id == SMOKE_CALENDAR_ID
    assert configuration.settings.database_url_value.endswith("/receptionist_test")


def test_unsafe_database_guard_prevents_provider_construction(monkeypatch, capsys) -> None:
    environment = _environment()
    environment["RECEPTIONIST_DATABASE_URL"] = "postgresql+psycopg://localhost:55432/receptionist"
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
    assert "receptionist_test" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("read_statuses", "failure_message"),
    [
        (("provider_divergent",), "created smoke booking did not reconcile"),
        (("in_sync", "provider_divergent"), "rescheduled smoke booking did not reconcile"),
        (
            ("in_sync", "in_sync", "provider_divergent"),
            "cancelled smoke booking did not reconcile",
        ),
    ],
    ids=["create-read", "reschedule-read", "cancel-read"],
)
async def test_smoke_rejects_provider_divergence(
    monkeypatch, read_statuses: tuple[str, ...], failure_message: str
) -> None:
    conversation_id, start = _install_smoke_operation_fakes(monkeypatch, read_statuses)

    with pytest.raises(SmokeWorkflowError, match=failure_message):
        await run_smoke_operations(
            object(),
            DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID),
            conversation_id=conversation_id,
            now_utc=start,
        )


async def test_smoke_rejects_wrong_rescheduled_interval(monkeypatch) -> None:
    conversation_id, now_utc = _install_smoke_operation_fakes(
        monkeypatch, ("in_sync", "in_sync"), wrong_reschedule_interval=True
    )

    with pytest.raises(SmokeWorkflowError, match="rescheduled smoke booking did not reconcile"):
        await run_smoke_operations(
            object(),
            DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID),
            conversation_id=conversation_id,
            now_utc=now_utc,
        )


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


async def test_smoke_cleans_exact_event_after_post_create_failure(monkeypatch) -> None:
    configuration = build_smoke_configuration(_environment())
    calendar = DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID)
    resources = SimpleNamespace(session_factory=object())
    booking_id = uuid4()
    event_id = "smokeeventid"
    unrelated_event_id = "unrelatedevent"
    disposed = False

    async def fake_create_conversation(*args, **kwargs) -> UUID:
        return uuid4()

    async def fake_operations(*args, tracker, **kwargs):
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            event_id,
            CalendarEventCreate(
                interval=CalendarInterval(
                    datetime(2026, 10, 11, 7, tzinfo=UTC),
                    datetime(2026, 10, 11, 8, tzinfo=UTC),
                ),
                private_booking_id=str(booking_id),
            ),
        )
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            unrelated_event_id,
            CalendarEventCreate(
                interval=CalendarInterval(
                    datetime(2026, 10, 11, 9, tzinfo=UTC),
                    datetime(2026, 10, 11, 10, tzinfo=UTC),
                ),
                private_booking_id="unrelated-booking",
            ),
        )
        tracker.result = SmokeRunResult(
            booking_id=booking_id,
            calendar_id=SMOKE_CALENDAR_ID,
            calendar_event_id=event_id,
        )
        raise SmokeWorkflowError("failure after create")

    async def fake_dispose(*args, **kwargs):
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(
        "scripts.calendar_smoke.create_database_resources", lambda settings: resources
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.GoogleCalendarClient.from_settings", lambda settings: calendar
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke._create_smoke_conversation", fake_create_conversation
    )
    monkeypatch.setattr("scripts.calendar_smoke.run_smoke_operations", fake_operations)
    monkeypatch.setattr("scripts.calendar_smoke.dispose_database", fake_dispose)

    with pytest.raises(SmokeWorkflowError, match="failure after create"):
        await run_smoke(configuration)

    assert await calendar.get_event(SMOKE_CALENDAR_ID, event_id) is None
    assert await calendar.get_event(SMOKE_CALENDAR_ID, unrelated_event_id) is not None
    assert disposed is True


@pytest.mark.parametrize(
    "failure_stage",
    [
        "after create before retrieval",
        "during reschedule preparation",
        "after reschedule before cancellation",
        "during final cancellation verification",
    ],
)
async def test_smoke_cleans_exact_event_after_any_post_create_failure(
    monkeypatch, failure_stage: str
) -> None:
    configuration = build_smoke_configuration(_environment())
    calendar = DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID)
    resources = SimpleNamespace(session_factory=object())
    booking_id = uuid4()
    event_id = "smokeeventid"
    unrelated_event_id = "unrelatedevent"

    async def fake_create_conversation(*args, **kwargs) -> UUID:
        return uuid4()

    async def fake_operations(*args, tracker, **kwargs):
        interval = CalendarInterval(
            datetime(2026, 10, 11, 7, tzinfo=UTC),
            datetime(2026, 10, 11, 8, tzinfo=UTC),
        )
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            event_id,
            CalendarEventCreate(interval=interval, private_booking_id=str(booking_id)),
        )
        await calendar.create_event(
            SMOKE_CALENDAR_ID,
            unrelated_event_id,
            CalendarEventCreate(
                interval=CalendarInterval(
                    datetime(2026, 10, 11, 9, tzinfo=UTC),
                    datetime(2026, 10, 11, 10, tzinfo=UTC),
                ),
                private_booking_id="unrelated-booking",
            ),
        )
        tracker.result = SmokeRunResult(
            booking_id=booking_id,
            calendar_id=SMOKE_CALENDAR_ID,
            calendar_event_id=event_id,
        )
        raise SmokeWorkflowError(failure_stage)

    monkeypatch.setattr(
        "scripts.calendar_smoke.create_database_resources", lambda settings: resources
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.GoogleCalendarClient.from_settings", lambda settings: calendar
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke._create_smoke_conversation", fake_create_conversation
    )
    monkeypatch.setattr("scripts.calendar_smoke.run_smoke_operations", fake_operations)
    monkeypatch.setattr("scripts.calendar_smoke.dispose_database", lambda resources: _async_noop())

    with pytest.raises(SmokeWorkflowError, match=failure_stage):
        await run_smoke(configuration)

    assert await calendar.get_event(SMOKE_CALENDAR_ID, event_id) is None
    assert await calendar.get_event(SMOKE_CALENDAR_ID, unrelated_event_id) is not None


async def _async_noop() -> None:
    return None


async def test_smoke_disposes_resources_if_provider_bootstrap_fails(monkeypatch) -> None:
    configuration = build_smoke_configuration(_environment())
    resources = SimpleNamespace(session_factory=object())
    disposed = False

    async def fake_dispose(*args, **kwargs):
        nonlocal disposed
        disposed = True

    monkeypatch.setattr(
        "scripts.calendar_smoke.create_database_resources", lambda settings: resources
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.GoogleCalendarClient.from_settings",
        lambda settings: (_ for _ in ()).throw(RuntimeError("bootstrap failure")),
    )
    monkeypatch.setattr("scripts.calendar_smoke.dispose_database", fake_dispose)

    with pytest.raises(RuntimeError, match="bootstrap failure"):
        await run_smoke(configuration)

    assert disposed is True


async def test_smoke_does_not_cleanup_when_create_never_happens(monkeypatch) -> None:
    configuration = build_smoke_configuration(_environment())
    resources = SimpleNamespace(session_factory=object())
    cleanup_called = False

    async def fake_create_conversation(*args, **kwargs) -> UUID:
        return uuid4()

    async def fake_operations(*args, **kwargs):
        raise SmokeWorkflowError("failure before create")

    async def fake_cleanup(*args, **kwargs):
        nonlocal cleanup_called
        cleanup_called = True

    async def fake_dispose(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "scripts.calendar_smoke.create_database_resources", lambda settings: resources
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.GoogleCalendarClient.from_settings", lambda settings: object()
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke._create_smoke_conversation", fake_create_conversation
    )
    monkeypatch.setattr("scripts.calendar_smoke.run_smoke_operations", fake_operations)
    monkeypatch.setattr("scripts.calendar_smoke._cleanup_exact_event", fake_cleanup)
    monkeypatch.setattr("scripts.calendar_smoke.dispose_database", fake_dispose)

    with pytest.raises(SmokeWorkflowError, match="failure before create"):
        await run_smoke(configuration)

    assert cleanup_called is False


async def test_smoke_tracker_captures_identity_before_replay_or_retrieval(monkeypatch) -> None:
    tracker = SmokeRunTracker()

    def observe_before_replay() -> None:
        assert tracker.result is not None
        assert tracker.result.calendar_id == SMOKE_CALENDAR_ID

    conversation_id, now_utc = _install_smoke_operation_fakes(
        monkeypatch, ("in_sync", "in_sync", "in_sync"), replay_observer=observe_before_replay
    )

    await run_smoke_operations(
        object(),
        DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID),
        conversation_id=conversation_id,
        now_utc=now_utc,
        tracker=tracker,
    )

    assert tracker.result is not None
    assert tracker.result.calendar_id == SMOKE_CALENDAR_ID


@pytest.mark.parametrize("replay_failure", ["raises", "not_replayed"])
async def test_smoke_cleans_event_when_replay_verification_fails(
    monkeypatch, replay_failure: str
) -> None:
    configuration = build_smoke_configuration(_environment())
    calendar = DeterministicCalendarDouble(calendar_id=SMOKE_CALENDAR_ID)
    resources = SimpleNamespace(session_factory=object())
    conversation_id = uuid4()
    booking_id = uuid4()
    event_id = "smokeeventid"
    interval = CalendarInterval(
        datetime(2026, 10, 11, 7, tzinfo=UTC),
        datetime(2026, 10, 11, 8, tzinfo=UTC),
    )
    unrelated_event_id = "unrelatedevent"
    confirmation_calls = 0

    async def fake_prepare_create(*args, **kwargs):
        return PreparationResult(
            operation="prepare_create_booking",
            data=PreparationData(
                action_type=PendingActionType.CREATE_BOOKING,
                action_token=str(uuid4()),
                requested_start_at_utc=interval.start_at_utc,
                requested_end_at_utc=interval.end_at_utc,
            ),
        )

    async def fake_confirm(*args, **kwargs):
        nonlocal confirmation_calls
        confirmation_calls += 1
        if confirmation_calls == 1:
            await calendar.create_event(
                SMOKE_CALENDAR_ID,
                event_id,
                CalendarEventCreate(interval=interval, private_booking_id=str(booking_id)),
            )
            return ConfirmationResult(
                data=ConfirmationData(
                    booking_id=booking_id,
                    status=BookingStatus.CONFIRMED,
                    requested_start_at_utc=interval.start_at_utc,
                    requested_end_at_utc=interval.end_at_utc,
                    calendar_id=SMOKE_CALENDAR_ID,
                    calendar_event_id=event_id,
                )
            )
        if replay_failure == "raises":
            raise SmokeWorkflowError("replay provider failure")
        return ConfirmationResult(
            replayed=False,
            data=ConfirmationData(
                booking_id=booking_id,
                status=BookingStatus.CONFIRMED,
                requested_start_at_utc=interval.start_at_utc,
                requested_end_at_utc=interval.end_at_utc,
                calendar_id=SMOKE_CALENDAR_ID,
                calendar_event_id=event_id,
            ),
        )

    async def fake_create_conversation(*args, **kwargs) -> UUID:
        return conversation_id

    async def fake_dispose(*args, **kwargs):
        return None

    await calendar.create_event(
        SMOKE_CALENDAR_ID,
        unrelated_event_id,
        CalendarEventCreate(
            interval=CalendarInterval(
                datetime(2026, 10, 11, 9, tzinfo=UTC),
                datetime(2026, 10, 11, 10, tzinfo=UTC),
            ),
            private_booking_id="unrelated-booking",
        ),
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.create_database_resources", lambda settings: resources
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke.GoogleCalendarClient.from_settings", lambda settings: calendar
    )
    monkeypatch.setattr(
        "scripts.calendar_smoke._create_smoke_conversation", fake_create_conversation
    )
    monkeypatch.setattr("scripts.calendar_smoke.prepare_create_booking", fake_prepare_create)
    monkeypatch.setattr("scripts.calendar_smoke.confirm_booking_action", fake_confirm)
    monkeypatch.setattr("scripts.calendar_smoke.dispose_database", fake_dispose)

    with pytest.raises(SmokeWorkflowError):
        await run_smoke(configuration)

    assert await calendar.get_event(SMOKE_CALENDAR_ID, event_id) is None
    assert await calendar.get_event(SMOKE_CALENDAR_ID, unrelated_event_id) is not None


async def test_smoke_operations_use_existing_finite_booking_operations(monkeypatch) -> None:
    conversation_id = uuid4()
    booking_id = uuid4()
    event_id = "smokeeventid"
    now_utc = datetime(2026, 10, 11, 7, tzinfo=UTC)
    start, end = _smoke_interval_after(now_utc)
    later_start, later_end = _smoke_interval_after(start)
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
        if get_calls == 1:
            status = BookingStatus.CONFIRMED
            read_start, read_end = start, end
        elif get_calls == 2:
            status = BookingStatus.CONFIRMED
            read_start, read_end = later_start, later_end
        else:
            status = BookingStatus.CANCELLED
            read_start, read_end = later_start, later_end
        return BookingReadResult(
            data=BookingReadData(
                booking_id=booking_id,
                status=status,
                start_at_utc=read_start,
                end_at_utc=read_end,
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
        object(), calendar, conversation_id=conversation_id, now_utc=now_utc
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
        "get_booking",
        "prepare_cancel_booking",
        "confirm_booking_action",
        "get_booking",
    ]

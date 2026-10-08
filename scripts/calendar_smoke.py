"""Guarded manual smoke flow for one dedicated non-production Calendar."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.application.booking import (
    BookingReadResult,
    CancelBookingRequest,
    ConfirmationResult,
    ConfirmBookingActionRequest,
    CreateBookingRequest,
    GetBookingRequest,
    PreparationResult,
    RescheduleBookingRequest,
    confirm_booking_action,
    get_booking,
    prepare_cancel_booking,
    prepare_create_booking,
    prepare_reschedule_booking,
)
from receptionist.core.config import (
    AppEnvironment,
    LogLevel,
    Settings,
    assert_safe_test_database,
)
from receptionist.db.models import Contact, Conversation
from receptionist.db.session import (
    create_database_resources,
    dispose_database,
)
from receptionist.domain.enums import (
    BookingStatus,
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
)
from receptionist.integrations.google_calendar import (
    CalendarClient,
    CalendarClientError,
    CalendarEventLifecycle,
    GoogleCalendarClient,
)
from receptionist.seed import seed_reference_data

SMOKE_CONFIRMATION = "DEDICATED_NON_PRODUCTION_ONLY"
SMOKE_ENVIRONMENT = "synthetic"
_SECONDARY_CALENDAR_SUFFIX = "@group.calendar.google.com"
_RIYADH = ZoneInfo("Asia/Riyadh")


class SmokeGuardError(RuntimeError):
    """A safe refusal raised before any provider client is constructed."""


class SmokeWorkflowError(RuntimeError):
    """A bounded failure in the synthetic smoke operation sequence."""


@dataclass(frozen=True, slots=True)
class SmokeConfiguration:
    """Validated smoke-only configuration with secret-safe representation."""

    calendar_id: str
    settings: Settings


@dataclass(frozen=True, slots=True)
class SmokeRunResult:
    """Opaque local identity needed for exact cleanup."""

    booking_id: UUID
    calendar_id: str
    calendar_event_id: str


@dataclass(slots=True)
class SmokeRunTracker:
    """Mutable exact identity captured as soon as create is reconciled."""

    result: SmokeRunResult | None = None


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise SmokeGuardError("dedicated Calendar credentials unavailable")
    return value


def build_smoke_configuration(environment: Mapping[str, str]) -> SmokeConfiguration:
    """Validate all smoke guards before constructing a provider client."""
    app_env = environment.get("RECEPTIONIST_APP_ENV", "").strip()
    if app_env != "test":
        raise SmokeGuardError("smoke requires the test application environment")
    if environment.get("RECEPTIONIST_CALENDAR_SMOKE_ENV", "").strip() != SMOKE_ENVIRONMENT:
        raise SmokeGuardError("explicit synthetic smoke environment is required")
    if environment.get("RECEPTIONIST_CALENDAR_SMOKE_CONFIRM", "").strip() != SMOKE_CONFIRMATION:
        raise SmokeGuardError("explicit smoke confirmation is required")

    calendar_id = environment.get("RECEPTIONIST_CALENDAR_SMOKE_CALENDAR_ID", "").strip()
    if (
        not calendar_id
        or calendar_id == "primary"
        or not calendar_id.endswith(_SECONDARY_CALENDAR_SUFFIX)
        or not calendar_id.removesuffix(_SECONDARY_CALENDAR_SUFFIX)
    ):
        raise SmokeGuardError("dedicated secondary Calendar ID is required")
    if calendar_id == environment.get("RECEPTIONIST_GOOGLE_CALENDAR_ID", "").strip():
        raise SmokeGuardError("smoke Calendar ID must be separate from the runtime Calendar ID")

    database_url = _required(environment, "RECEPTIONIST_DATABASE_URL")
    client_id = _required(environment, "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _required(environment, "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = _required(environment, "RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN")
    try:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            app_env=cast(AppEnvironment, app_env),
            log_level=cast(LogLevel, environment.get("RECEPTIONIST_LOG_LEVEL", "INFO")),
            database_url=SecretStr(database_url),
            google_calendar_id=calendar_id,
            google_oauth_client_id=SecretStr(client_id),
            google_oauth_client_secret=SecretStr(client_secret),
            google_oauth_refresh_token=SecretStr(refresh_token),
            google_calendar_request_timeout_seconds=float(
                environment.get("RECEPTIONIST_GOOGLE_CALENDAR_REQUEST_TIMEOUT_SECONDS", 10.0)
            ),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SmokeGuardError("required smoke runtime settings are invalid") from exc
    try:
        assert_safe_test_database(settings)
    except RuntimeError as exc:
        raise SmokeGuardError(str(exc)) from exc
    if not settings.google_calendar_configured:
        raise SmokeGuardError("dedicated Calendar credentials unavailable")
    return SmokeConfiguration(calendar_id=calendar_id, settings=settings)


def _smoke_interval_after(after_utc: datetime) -> tuple[datetime, datetime]:
    """Choose the next synthetic business-hour slot without provider state."""
    after_local = after_utc.astimezone(_RIYADH)
    for day_offset in range(1, 15):
        candidate_date = after_local.date() + timedelta(days=day_offset)
        start_hour = 15 if candidate_date.weekday() == 4 else 10
        start_local = datetime.combine(
            candidate_date,
            time(hour=start_hour),
            tzinfo=_RIYADH,
        )
        start_utc = start_local.astimezone(UTC)
        end_utc = (start_local + timedelta(hours=1)).astimezone(UTC)
        if start_utc > after_utc + timedelta(minutes=120):
            return start_utc, end_utc
    raise SmokeWorkflowError("unable to choose a bounded synthetic smoke interval")


async def run_smoke_operations(
    session_factory: async_sessionmaker[AsyncSession],
    calendar: CalendarClient,
    *,
    conversation_id: UUID,
    now_utc: datetime,
    tracker: SmokeRunTracker | None = None,
) -> SmokeRunResult:
    """Exercise the existing finite booking operations in the smoke sequence."""
    create_start, create_end = _smoke_interval_after(now_utc)
    reschedule_start, reschedule_end = _smoke_interval_after(create_start)
    run_id = uuid4()

    prepared_create = await prepare_create_booking(
        session_factory,
        calendar,
        CreateBookingRequest(
            conversation_id=conversation_id,
            service_selector="plumbing",
            service_area_selector="al_olaya",
            requested_start_at=create_start,
            requested_end_at=create_end,
            idempotency_key=f"smoke-create-{run_id}",
        ),
        now_utc=now_utc,
    )
    if not isinstance(prepared_create, PreparationResult):
        raise SmokeWorkflowError("synthetic create preparation was rejected")
    create_confirmation_request = ConfirmBookingActionRequest(
        conversation_id=conversation_id,
        action_type=prepared_create.data.action_type,
        action_token=prepared_create.data.action_token,
        idempotency_key=f"smoke-confirm-create-{run_id}",
    )
    confirmed = await confirm_booking_action(
        session_factory, calendar, create_confirmation_request, now_utc=now_utc
    )
    if not isinstance(confirmed, ConfirmationResult):
        raise SmokeWorkflowError("synthetic create confirmation was rejected")
    replay = await confirm_booking_action(
        session_factory, calendar, create_confirmation_request, now_utc=now_utc
    )
    if not isinstance(replay, ConfirmationResult) or not replay.replayed:
        raise SmokeWorkflowError("same-key confirmation replay was not observed")

    booking_id = confirmed.data.booking_id
    calendar_id = confirmed.data.calendar_id
    event_id = confirmed.data.calendar_event_id
    if calendar_id is None or event_id is None:
        raise SmokeWorkflowError("confirmed smoke booking has no Calendar event identity")
    smoke_result = SmokeRunResult(
        booking_id=booking_id,
        calendar_id=calendar_id,
        calendar_event_id=event_id,
    )
    if tracker is not None:
        tracker.result = smoke_result
    read = await _read_booking(
        session_factory, calendar, conversation_id=conversation_id, booking_id=booking_id
    )
    if (
        not isinstance(read, BookingReadResult)
        or read.data.status is not BookingStatus.CONFIRMED
        or read.data.reconciliation_status != "in_sync"
    ):
        raise SmokeWorkflowError("created smoke booking did not reconcile")

    prepared_reschedule = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=reschedule_start,
            requested_end_at=reschedule_end,
            idempotency_key=f"smoke-reschedule-{run_id}",
        ),
        now_utc=now_utc,
    )
    if not isinstance(prepared_reschedule, PreparationResult):
        raise SmokeWorkflowError("synthetic reschedule preparation was rejected")
    rescheduled = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=prepared_reschedule.data.action_type,
            action_token=prepared_reschedule.data.action_token,
            idempotency_key=f"smoke-confirm-reschedule-{run_id}",
        ),
        now_utc=now_utc,
    )
    if not isinstance(rescheduled, ConfirmationResult):
        raise SmokeWorkflowError("synthetic reschedule confirmation was rejected")
    rescheduled_read = await _read_booking(
        session_factory, calendar, conversation_id=conversation_id, booking_id=booking_id
    )
    if (
        not isinstance(rescheduled_read, BookingReadResult)
        or rescheduled_read.data.status is not BookingStatus.CONFIRMED
        or rescheduled_read.data.start_at_utc != reschedule_start
        or rescheduled_read.data.end_at_utc != reschedule_end
        or rescheduled_read.data.reconciliation_status != "in_sync"
    ):
        raise SmokeWorkflowError("rescheduled smoke booking did not reconcile")

    prepared_cancel = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"smoke-cancel-{run_id}",
        ),
    )
    if not isinstance(prepared_cancel, PreparationResult):
        raise SmokeWorkflowError("synthetic cancellation preparation was rejected")
    cancelled = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=prepared_cancel.data.action_type,
            action_token=prepared_cancel.data.action_token,
            idempotency_key=f"smoke-confirm-cancel-{run_id}",
        ),
        now_utc=now_utc,
    )
    if not isinstance(cancelled, ConfirmationResult):
        raise SmokeWorkflowError("synthetic cancellation confirmation was rejected")
    final_read = await _read_booking(
        session_factory, calendar, conversation_id=conversation_id, booking_id=booking_id
    )
    if (
        not isinstance(final_read, BookingReadResult)
        or final_read.data.status is not BookingStatus.CANCELLED
        or final_read.data.reconciliation_status != "in_sync"
    ):
        raise SmokeWorkflowError("cancelled smoke booking did not reconcile")
    return smoke_result


async def _read_booking(
    session_factory: async_sessionmaker[AsyncSession],
    calendar: CalendarClient,
    *,
    conversation_id: UUID,
    booking_id: UUID,
) -> BookingReadResult | object:
    async with session_factory() as session:
        return await get_booking(
            session,
            calendar,
            GetBookingRequest(conversation_id=conversation_id, booking_id=booking_id),
        )


async def _cleanup_exact_event(
    calendar: CalendarClient,
    *,
    calendar_id: str,
    event_id: str,
    booking_id: UUID,
) -> None:
    """Cancel only the exact smoke event with the expected private marker."""
    event = await calendar.get_event(calendar_id, event_id)
    if event is None or event.lifecycle is CalendarEventLifecycle.CANCELLED:
        return
    if (
        event.calendar_id != calendar_id
        or event.event_id != event_id
        or event.lifecycle is not CalendarEventLifecycle.ACTIVE
        or event.private_booking_id != str(booking_id)
        or event.etag is None
    ):
        raise SmokeWorkflowError("smoke cleanup identity did not match")
    await calendar.cancel_event(calendar_id, event_id, event.etag)


async def _create_smoke_conversation(
    session_factory: async_sessionmaker[AsyncSession], app_env: str
) -> UUID:
    async with session_factory.begin() as session:
        await seed_reference_data(session, app_env)
        contact = Contact(phone_e164=None, display_name=None, email=None)
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
        return conversation.id


async def run_smoke(configuration: SmokeConfiguration) -> None:
    """Construct the real client only after guards and execute the bounded flow."""
    resources = create_database_resources(configuration.settings)
    try:
        calendar = GoogleCalendarClient.from_settings(configuration.settings)
        tracker = SmokeRunTracker()
        try:
            conversation_id = await _create_smoke_conversation(
                resources.session_factory, configuration.settings.app_env
            )
            await run_smoke_operations(
                resources.session_factory,
                calendar,
                conversation_id=conversation_id,
                now_utc=datetime.now(UTC),
                tracker=tracker,
            )
        finally:
            if tracker.result is not None:
                await _cleanup_exact_event(
                    calendar,
                    calendar_id=tracker.result.calendar_id,
                    event_id=tracker.result.calendar_event_id,
                    booking_id=tracker.result.booking_id,
                )
    finally:
        await dispose_database(resources)


def main() -> int:
    """Run the manually invoked smoke path with bounded output."""
    try:
        configuration = build_smoke_configuration(os.environ)
    except SmokeGuardError as exc:
        if str(exc) == "dedicated Calendar credentials unavailable":
            print("not run: dedicated Calendar credentials unavailable")
        else:
            print(f"calendar smoke refused: {exc}")
        return 2
    try:
        asyncio.run(run_smoke(configuration))
    except CalendarClientError as exc:
        print(f"calendar smoke failed: {exc.code.value}")
        return 1
    except SmokeWorkflowError as exc:
        print(f"calendar smoke failed: {exc}")
        return 1
    except Exception:
        print("calendar smoke failed: bounded unexpected failure")
        return 1
    print("calendar smoke passed: dedicated synthetic workflow reconciled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

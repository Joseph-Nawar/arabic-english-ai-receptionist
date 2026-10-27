from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from receptionist.application.booking import (
    CancelBookingRequest,
    ConfirmationData,
    ConfirmationResult,
    ConfirmBookingActionRequest,
    CreateBookingRequest,
    PreparationResult,
    RescheduleBookingRequest,
    confirm_booking_action,
    prepare_cancel_booking,
    prepare_create_booking,
    prepare_reschedule_booking,
)
from receptionist.application.crm_booking_outcome import (
    BookingOutcome,
    BookingOutcomeErrorCode,
    BookingOutcomeNextAction,
    BookingOutcomeRequest,
    record_booking_outcome,
)
from receptionist.core.config import Settings
from receptionist.db.models import (
    AuditEvent,
    Booking,
    Contact,
    Conversation,
    CRMContactLink,
    CRMContactLinkState,
    Service,
    ToolExecution,
)
from receptionist.domain.enums import (
    BookingStatus,
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
    PendingActionType,
    ToolExecutionStatus,
)
from receptionist.domain.identity import CRMContactLinkProvenance
from receptionist.seed import seed_reference_data
from tests.support.calendar_double import DeterministicCalendarDouble

pytestmark = pytest.mark.integration

PORTAL_ID = "123456"
AUTH_SENTINEL = "private-mock-service-key"
TEST_DATABASE_URL = "postgresql+psycopg://receptionist@localhost/receptionist_test"
FINISHED_AT = datetime(2026, 10, 25, 16, 0, 0, 123456, tzinfo=UTC)
START_AT = datetime(2026, 10, 26, 7, 0, 0, 123456, tzinfo=UTC)
END_AT = datetime(2026, 10, 26, 8, 0, 0, 654321, tzinfo=UTC)
CUSTOMER_NAME = "Private Customer Name"
CUSTOMER_EMAIL = "private-customer@example.invalid"
TRANSCRIPT = "Private transcript marker"
PROVIDER_PAYLOAD = "Private provider payload marker"


@dataclass(frozen=True)
class Source:
    booking_id: uuid.UUID
    source_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    service_id: uuid.UUID


def _settings(*, app_env: str = "test", portal_id: str = PORTAL_ID) -> Settings:
    return Settings(
        _env_file=None,
        app_env=app_env,
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        hubspot_service_key=AUTH_SENTINEL,
        hubspot_portal_id=portal_id,
        hubspot_request_timeout_seconds=1.0,
    )


def _provider_contact(
    provider_id: str,
    *,
    email: str | None,
    phone: str | None,
    archived: bool = False,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "properties": {"email": email, "phone": phone, "mobilephone": None},
        "archived": archived,
    }


class MockHubSpot:
    def __init__(self, *, email: str | None, phone: str | None) -> None:
        self.portal_id = int(PORTAL_ID)
        self.contact_id = str(900_000_000 + uuid.uuid4().int % 99_000_000)
        self.contact_body = _provider_contact(self.contact_id, email=email, phone=phone)
        self.search_contact_body: dict[str, object] | None = None
        self.contact_status = 200
        self.account_status = 200
        self.note_status = 201
        self.note_response: object | None = None
        self.malformed_note_response = False
        self.note_response_associations = True
        self.note_contact_ids: list[str] = [self.contact_id]
        self.note_archived = False
        self.note_body_override: str | None = None
        self.note_timestamp_override: str | None = None
        self.omit_note_response_properties = False
        self.note_exception: BaseException | None = None
        self.save_note_before_exception = False
        self.before_note_response: Callable[[], Awaitable[None]] | None = None
        self.search_status = 200
        self.search_exception: BaseException | None = None
        self.search_response: object | None = None
        self.search_incomplete = False
        self.search_visible_after = 0
        self.search_count = 0
        self.before_note_search: Callable[[], Awaitable[None]] | None = None
        self.note_read_status = 200
        self.note_read_fail_on_count: int | None = None
        self.note_read_exception: BaseException | None = None
        self.note_read_override: dict[str, object] | None = None
        self.association_put_status = 204
        self.association_put_exception: BaseException | None = None
        self.association_put_after_mutation = False
        self.before_association_put: Callable[[], Awaitable[None]] | None = None
        self.requests: list[httpx.Request] = []
        self.note_payloads: list[dict[str, object]] = []
        self.note_create_count = 0
        self.note_search_count = 0
        self.note_read_count = 0
        self.association_put_count = 0
        self.note_id = str(800_000_000 + uuid.uuid4().int % 99_000_000)
        self.saved_note: dict[str, object] | None = None
        self.saved_notes: dict[str, dict[str, object]] = {}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/account-info/2026-03/details":
            return httpx.Response(self.account_status, json={"portalId": self.portal_id})

        if request.method == "GET" and request.url.params.get("idProperty") == "email":
            email = unquote(request.url.path.rsplit("/", maxsplit=1)[-1]).casefold()
            props = self.contact_body["properties"]
            assert isinstance(props, dict)
            if props.get("email") != email:
                return httpx.Response(404, json={"message": "private provider error"})
            return httpx.Response(200, json=self.contact_body)

        if request.method == "POST" and request.url.path.endswith("/contacts/search"):
            return httpx.Response(
                200,
                json={"total": 1, "results": [self.search_contact_body or self.contact_body]},
            )

        if request.method == "POST" and request.url.path == "/crm/objects/2026-09/notes/search":
            self.note_search_count += 1
            if self.before_note_search is not None:
                await self.before_note_search()
            if self.search_exception is not None:
                raise self.search_exception
            if self.search_response is not None:
                return httpx.Response(self.search_status, json=self.search_response)
            query = json.loads(request.content).get("query")
            visible = self.note_search_count > self.search_visible_after
            rows = []
            if visible and isinstance(query, str):
                for note in self.saved_notes.values():
                    properties = note.get("properties")
                    body = properties.get("hs_note_body") if isinstance(properties, dict) else None
                    if isinstance(body, str) and query in body:
                        rows.append(
                            {
                                "id": note["id"],
                                "properties": properties,
                                "archived": note.get("archived", False),
                            }
                        )
            response_body: dict[str, object] = {"total": len(rows), "results": rows[:5]}
            if self.search_incomplete:
                response_body["paging"] = {"next": {"after": "100"}}
            return httpx.Response(self.search_status, json=response_body)

        if request.method == "GET" and request.url.path.endswith(f"/contacts/{self.contact_id}"):
            return httpx.Response(self.contact_status, json=self.contact_body)

        if request.method == "POST" and request.url.path == "/crm/objects/2026-09/notes":
            self.note_create_count += 1
            self.note_id = str(800_000_000 + uuid.uuid4().int % 99_000_000)
            payload = json.loads(request.content)
            self.note_payloads.append(payload)
            if self.note_exception is not None and not self.save_note_before_exception:
                raise self.note_exception
            if self.note_status == 429:
                return httpx.Response(429, json={"message": "private rate limit"})
            properties = payload["properties"]
            self.saved_note = {
                "id": self.note_id,
                "properties": {
                    "hs_note_body": self.note_body_override
                    if self.note_body_override is not None
                    else properties["hs_note_body"],
                    "hs_timestamp": self.note_timestamp_override
                    if self.note_timestamp_override is not None
                    else properties["hs_timestamp"],
                },
                "archived": self.note_archived,
                "associations": {
                    "contacts": {
                        "results": [{"id": contact_id} for contact_id in self.note_contact_ids]
                    }
                },
            }
            self.saved_notes[self.note_id] = self.saved_note
            if self.before_note_response is not None:
                await self.before_note_response()
            if self.note_exception is not None:
                raise self.note_exception
            if self.malformed_note_response:
                return httpx.Response(201, content=b"not-json")
            if self.note_response is not None:
                return httpx.Response(self.note_status, json=self.note_response)
            if self.omit_note_response_properties:
                response_body: dict[str, object] = {"id": self.note_id, "properties": {}}
            else:
                response_body = dict(self.saved_note)
            if not self.note_response_associations:
                response_body.pop("associations", None)
            if self.note_response_associations and self.note_contact_ids == []:
                response_body["associations"] = {"contacts": {"results": []}}
            return httpx.Response(self.note_status, json=response_body)

        path_parts = request.url.path.rstrip("/").split("/")
        if (
            request.method == "PUT"
            and len(path_parts) >= 4
            and path_parts[-4] == "associations"
            and path_parts[-3] == "contact"
        ):
            self.association_put_count += 1
            if self.before_association_put is not None:
                await self.before_association_put()
            note_id = path_parts[-5]
            contact_id = path_parts[-2]
            note = self.saved_notes.get(note_id)
            if note is None:
                return httpx.Response(404, json={"message": "private missing note"})
            if (
                self.association_put_exception is not None
                and not self.association_put_after_mutation
            ):
                raise self.association_put_exception
            note["associations"] = {"contacts": {"results": [{"id": contact_id}]}}
            if self.association_put_exception is not None:
                raise self.association_put_exception
            return httpx.Response(self.association_put_status)

        if request.method == "GET" and len(path_parts) == 6 and path_parts[-2] == "notes":
            self.note_read_count += 1
            note_id = path_parts[-1]
            if self.note_read_fail_on_count == self.note_read_count:
                return httpx.Response(503, json={"message": "private read failure"})
            if self.note_read_exception is not None:
                raise self.note_read_exception
            if self.note_read_status != 200:
                return httpx.Response(self.note_read_status, json={"message": "private read error"})
            note = self.saved_notes.get(note_id)
            if note is None:
                return httpx.Response(404, json={"message": "private missing note"})
            if self.note_read_override is not None:
                return httpx.Response(200, json=self.note_read_override)
            return httpx.Response(200, json=note)

        return httpx.Response(404, json={"message": "private unexpected request"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def _create_source(
    session_factory,
    *,
    action: str = "create_booking",
    historical_status: BookingStatus = BookingStatus.CONFIRMED,
    historical_start: datetime | None = START_AT,
    historical_end: datetime | None = END_AT,
    execution_status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED,
    tool_name: str = "confirm_booking_action",
    create_link: bool = True,
    link_state: CRMContactLinkState = CRMContactLinkState.ACTIVE,
    link_portal_id: str = PORTAL_ID,
    link_provider_id: str | None = None,
    linked_by: CRMContactLinkProvenance = CRMContactLinkProvenance.PHONE_E164,
    source_contact_id: uuid.UUID | None = None,
    email: str | None = None,
    finished_at: datetime | None = FINISHED_AT,
) -> Source:
    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        service = await session.scalar(select(Service).where(Service.code == "plumbing"))
        assert service is not None
        provider_id = str(900_000_000 + uuid.uuid4().int % 99_000_000)
        contact = Contact(
            phone_e164=f"+96650{uuid.uuid4().int % 10_000_000:07d}",
            display_name=CUSTOMER_NAME,
            email=email,
        )
        session.add(contact)
        await session.flush()
        if source_contact_id is not None:
            session.add(
                Contact(
                    id=source_contact_id,
                    phone_e164=f"+96655{uuid.uuid4().int % 10_000_000:07d}",
                )
            )
            await session.flush()
        conversation = Conversation(
            contact_id=source_contact_id or contact.id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
            recent_summary=TRANSCRIPT,
        )
        session.add(conversation)
        await session.flush()
        booking = Booking(
            contact_id=contact.id,
            service_id=service.id,
            status=historical_status,
            start_at=START_AT,
            end_at=END_AT,
            calendar_id="private-calendar-id",
            calendar_event_id=f"private-event-{uuid.uuid4().hex}",
            booking_data={
                "schema_version": 1,
                "last_operation": action,
                "requirements": {"private_note": PROVIDER_PAYLOAD},
                "provider_payload": {"private": PROVIDER_PAYLOAD},
            },
            confirmed_at=FINISHED_AT,
        )
        session.add(booking)
        await session.flush()
        if create_link:
            session.add(
                CRMContactLink(
                    contact_id=contact.id,
                    hubspot_portal_id=link_portal_id,
                    hubspot_contact_id=link_provider_id or provider_id,
                    linked_by=linked_by,
                    state=link_state,
                )
            )
        execution = ToolExecution(
            conversation_id=conversation.id,
            tool_name=tool_name,
            status=execution_status,
            sanitized_arguments={
                "conversation_id": str(conversation.id),
                "action_type": action,
                "action_token": str(uuid.uuid4()),
            },
            sanitized_result=ConfirmationResult(
                data=ConfirmationData(
                    booking_id=booking.id,
                    status=historical_status,
                    requested_start_at_utc=historical_start,
                    requested_end_at_utc=historical_end,
                    calendar_id="private-calendar-id",
                    calendar_event_id=f"private-result-event-{uuid.uuid4().hex}",
                )
            ).model_dump(mode="json"),
            finished_at=finished_at,
        )
        session.add(execution)
        await session.flush()
        if create_link:
            link = await session.get(CRMContactLink, contact.id)
            assert link is not None
        source = Source(booking.id, execution.id, conversation.id, contact.id, service.id)
    return source


async def _add_source_execution(
    session_factory,
    source: Source,
    *,
    action: str,
    historical_status: BookingStatus,
    finished_at: datetime,
    historical_start: datetime | None,
    historical_end: datetime | None,
) -> uuid.UUID:
    async with session_factory.begin() as session:
        execution = ToolExecution(
            conversation_id=source.conversation_id,
            tool_name="confirm_booking_action",
            status=ToolExecutionStatus.SUCCEEDED,
            sanitized_arguments={"action_type": action, "action_token": str(uuid.uuid4())},
            sanitized_result=ConfirmationResult(
                data=ConfirmationData(
                    booking_id=source.booking_id,
                    status=historical_status,
                    requested_start_at_utc=historical_start,
                    requested_end_at_utc=historical_end,
                    calendar_id="private-calendar-id",
                    calendar_event_id=f"private-result-event-{uuid.uuid4().hex}",
                )
            ).model_dump(mode="json"),
            finished_at=finished_at,
        )
        session.add(execution)
        await session.flush()
        return execution.id


async def _run(session_factory, source: Source, provider: MockHubSpot, *, settings=None):
    old_provider_id = provider.contact_id
    provider.contact_id = str(await _linked_provider_id(session_factory, source.contact_id))
    provider.contact_body["id"] = provider.contact_id
    if provider.search_contact_body is not None:
        provider.search_contact_body["id"] = provider.contact_id
    provider.note_contact_ids = [
        provider.contact_id if contact_id == old_provider_id else contact_id
        for contact_id in provider.note_contact_ids
    ]
    return await record_booking_outcome(
        session_factory,
        BookingOutcomeRequest(
            booking_id=source.booking_id,
            source_tool_execution_id=source.source_id,
        ),
        settings or _settings(),
        transport=provider.transport(),
    )


async def _linked_provider_id(session_factory, contact_id: uuid.UUID) -> str:
    async with session_factory() as session:
        link = await session.get(CRMContactLink, contact_id)
        return "missing" if link is None else link.hubspot_contact_id


async def _bind_provider_identity(session_factory, source: Source, provider: MockHubSpot) -> None:
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164


async def _leave_possible_note(
    session_factory,
    source: Source,
    provider: MockHubSpot,
    *,
    saved_before_failure: bool = True,
    failure: BaseException | None = None,
) -> None:
    provider.note_exception = failure or httpx.ReadTimeout("private post timeout")
    provider.save_note_before_exception = saved_before_failure
    first = await _run(session_factory, source, provider)
    assert first.outcome is BookingOutcome.UNCERTAIN
    provider.note_exception = None


async def _crm_execution(session_factory, source: Source) -> ToolExecution | None:
    from receptionist.application.crm_booking_outcome import _note_idempotency_key

    async with session_factory() as session:
        return await session.scalar(
            select(ToolExecution).where(
                ToolExecution.idempotency_key == _note_idempotency_key(source.source_id)
            )
        )


async def test_create_note_uses_exact_structured_body_timestamp_and_verified_association(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    expected_marker = f"booking-execution:{source.source_id}"
    expected_body = "\n".join(
        (
            "Booking operation: create_booking",
            "Outcome: confirmed",
            f"Booking reference: {source.booking_id}",
            "Requested start (UTC): 2026-10-26T07:00:00.123456Z",
            "Requested end (UTC): 2026-10-26T08:00:00.654321Z",
            f"Source operation: {expected_marker}",
        )
    )
    assert result.outcome is BookingOutcome.WRITTEN
    assert result.hubspot_note_id == provider.note_id
    assert provider.note_create_count == 1
    assert provider.note_payloads[0] == {
        "properties": {
            "hs_timestamp": "2026-10-25T16:00:00.123456Z",
            "hs_note_body": expected_body,
        },
        "associations": [
            {
                "to": {"id": provider.contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    assert set(provider.note_payloads[0]["properties"]) == {"hs_timestamp", "hs_note_body"}
    assert expected_marker in expected_body
    assert all(
        request.headers.get("authorization") == f"Bearer {AUTH_SENTINEL}"
        for request in provider.requests
    )


@pytest.mark.parametrize(
    ("action", "status", "expected_action", "expected_outcome"),
    [
        ("reschedule_booking", BookingStatus.CONFIRMED, "reschedule_booking", "confirmed"),
        ("cancel_booking", BookingStatus.CANCELLED, "cancel_booking", "cancelled"),
    ],
)
async def test_reschedule_and_cancel_notes_use_historical_action_and_status(
    session_factory,
    action: str,
    status: BookingStatus,
    expected_action: str,
    expected_outcome: str,
) -> None:
    source = await _create_source(
        session_factory,
        action=action,
        historical_status=status,
        historical_start=None,
        historical_end=None,
    )
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    body = provider.note_payloads[0]["properties"]["hs_note_body"]
    assert result.outcome is BookingOutcome.WRITTEN
    assert body == "\n".join(
        (
            f"Booking operation: {expected_action}",
            f"Outcome: {expected_outcome}",
            f"Booking reference: {source.booking_id}",
            f"Source operation: booking-execution:{source.source_id}",
        )
    )
    assert provider.note_payloads[0]["properties"]["hs_timestamp"] == (
        "2026-10-25T16:00:00.123456Z"
    )


@pytest.mark.parametrize(
    "status",
    [ToolExecutionStatus.STARTED, ToolExecutionStatus.FAILED, ToolExecutionStatus.REJECTED],
)
async def test_invalid_source_execution_never_creates_a_note(
    session_factory, status: ToolExecutionStatus
) -> None:
    source = await _create_source(session_factory, execution_status=status)
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.SOURCE_EXECUTION_INVALID
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


@pytest.mark.parametrize("tool_name", ["resolve_crm_contact", "confirm_other_action"])
async def test_wrong_source_tool_never_creates_a_note(session_factory, tool_name: str) -> None:
    source = await _create_source(session_factory, tool_name=tool_name)
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.SOURCE_EXECUTION_INVALID
    assert provider.note_create_count == 0


async def test_missing_verified_mapping_returns_safe_skip_without_provider_calls(
    session_factory,
) -> None:
    source = await _create_source(session_factory, create_link=False)
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert result.hubspot_note_id is None
    assert provider.requests == []
    assert await _crm_execution(session_factory, source) is None


async def test_malformed_provider_mapping_id_returns_safe_skip_without_provider_calls(
    session_factory,
) -> None:
    source = await _create_source(
        session_factory,
        link_provider_id=f"abc{uuid.uuid4().hex}",
    )
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert result.error_code is BookingOutcomeErrorCode.NO_VERIFIED_CONTACT
    assert provider.requests == []
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


@pytest.mark.parametrize(
    ("link_state", "link_portal_id"),
    [(CRMContactLinkState.STALE, PORTAL_ID), (CRMContactLinkState.ACTIVE, "999999")],
)
async def test_stale_or_wrong_portal_mapping_is_not_used(
    session_factory, link_state: CRMContactLinkState, link_portal_id: str
) -> None:
    source = await _create_source(
        session_factory,
        link_state=link_state,
        link_portal_id=link_portal_id,
    )
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert provider.requests == []


@pytest.mark.parametrize(("archived", "contact_status"), [(True, 200), (False, 404)])
async def test_archived_or_missing_provider_contact_skips_note(
    session_factory, archived: bool, contact_status: int
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.contact_body["archived"] = archived
    provider.contact_status = contact_status
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_wrong_booking_id_never_creates_a_note(session_factory) -> None:
    source = await _create_source(session_factory)
    async with session_factory.begin() as session:
        original = await session.get(Booking, source.booking_id)
        assert original is not None
        other_booking = Booking(
            contact_id=original.contact_id,
            service_id=original.service_id,
            status=BookingStatus.CONFIRMED,
            start_at=START_AT + timedelta(days=2),
            end_at=END_AT + timedelta(days=2),
            calendar_id="other-calendar",
            calendar_event_id=f"other-event-{uuid.uuid4().hex}",
            booking_data={"last_operation": "create_booking"},
            confirmed_at=FINISHED_AT,
        )
        session.add(other_booking)
        await session.flush()
        wrong_booking_id = other_booking.id
    provider = MockHubSpot(email=None, phone=None)

    result = await record_booking_outcome(
        session_factory,
        BookingOutcomeRequest(
            booking_id=wrong_booking_id,
            source_tool_execution_id=source.source_id,
        ),
        _settings(),
        transport=provider.transport(),
    )

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.HISTORICAL_RESULT_MISMATCH
    assert provider.note_create_count == 0


async def test_cross_contact_source_ownership_never_creates_a_note(session_factory) -> None:
    source = await _create_source(session_factory, source_contact_id=uuid.uuid4())
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.CONTACT_OWNERSHIP_CONFLICT
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_invalid_historical_action_never_creates_a_note(session_factory) -> None:
    source = await _create_source(session_factory)
    async with session_factory.begin() as session:
        execution = await session.get(ToolExecution, source.source_id)
        assert execution is not None
        execution.sanitized_arguments = {"action_type": "delete_booking"}
    provider = MockHubSpot(email=None, phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.SOURCE_EXECUTION_INVALID
    assert provider.note_create_count == 0


async def test_wrong_portal_preflight_returns_not_configured_without_note(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.portal_id = 777777
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.NOT_CONFIGURED
    assert result.error_code is BookingOutcomeErrorCode.ACCOUNT_MISMATCH
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_mapped_identity_mismatch_skips_note(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone="+966551234567")

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_by_id_contact_identity_conflict_cannot_be_masked_by_search(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = "+966551234567"
        provider.search_contact_body = _provider_contact(
            provider.contact_id,
            email=None,
            phone=contact.phone_e164,
        )

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT
    assert provider.note_create_count == 0


async def test_verified_email_provenance_is_rechecked_by_fresh_provider_reads(
    session_factory,
) -> None:
    source = await _create_source(
        session_factory,
        linked_by=CRMContactLinkProvenance.VERIFIED_EMAIL,
        email="trusted@example.invalid",
    )
    provider = MockHubSpot(email="trusted@example.invalid", phone=None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.WRITTEN
    assert any(request.url.params.get("idProperty") == "email" for request in provider.requests)


async def test_account_preflight_failure_never_creates_note(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.account_status = 403
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.NOT_CONFIGURED
    assert result.error_code is BookingOutcomeErrorCode.PERMISSION_DENIED
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_production_mock_transport_does_not_activate_note_writes(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider, settings=_settings(app_env="production"))

    assert result.outcome is BookingOutcome.NOT_CONFIGURED
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_durable_claim_is_committed_before_note_post(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    async def inspect_committed_claim() -> None:
        execution = await _crm_execution(session_factory, source)
        assert execution is not None
        assert execution.status is ToolExecutionStatus.STARTED
        assert execution.sanitized_result is None
        assert execution.sanitized_arguments == {
            "source_tool_execution_id": str(source.source_id),
            "booking_id": str(source.booking_id),
            "contact_id": str(source.contact_id),
            "conversation_id": str(source.conversation_id),
            "hubspot_portal_id": PORTAL_ID,
            "hubspot_contact_id": provider.contact_id,
            "operation_type": "record_booking_outcome",
            "historical_action": "create_booking",
            "historical_status": "confirmed",
            "requested_start_at_utc": "2026-10-26T07:00:00.123456Z",
            "requested_end_at_utc": "2026-10-26T08:00:00.654321Z",
            "source_finished_at_utc": "2026-10-25T16:00:00.123456Z",
            "source_marker": f"booking-execution:{source.source_id}",
        }

    provider.before_note_response = inspect_committed_claim

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.WRITTEN


async def test_source_evidence_is_revalidated_after_claim_before_note_post(
    session_factory, monkeypatch
) -> None:
    from receptionist.application import crm_booking_outcome as booking_outcome

    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164
    original_binding_check = booking_outcome._binding_is_current

    async def invalidate_source_after_claim(
        current_session_factory,
        evidence,
        binding,
        *,
        expected_portal_id,
    ) -> bool:
        async with current_session_factory.begin() as session:
            execution = await session.get(ToolExecution, evidence.source_tool_execution_id)
            assert execution is not None
            execution.status = ToolExecutionStatus.FAILED
        return await original_binding_check(
            current_session_factory,
            evidence,
            binding,
            expected_portal_id=expected_portal_id,
        )

    monkeypatch.setattr(
        booking_outcome,
        "_binding_is_current",
        invalidate_source_after_claim,
    )

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.SOURCE_EXECUTION_INVALID
    assert provider.note_create_count == 0


async def test_note_response_without_associations_is_verified_with_get_note(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.note_response_associations = False
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.WRITTEN
    assert [
        request.method
        for request in provider.requests
        if request.url.path.endswith("/notes") or "/notes/" in request.url.path
    ] == ["POST", "GET"]


@pytest.mark.parametrize(
    "contact_ids",
    [["unrelated-contact-id"], ["current-contact-id", "unrelated-contact-id"]],
)
async def test_missing_or_conflicting_note_association_remains_uncertain(
    session_factory, contact_ids: list[str]
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.note_contact_ids = [
        provider.contact_id if item == "current-contact-id" else item for item in contact_ids
    ]
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.hubspot_note_id is None
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.note_create_count == 1


@pytest.mark.parametrize(
    ("archived", "timestamp"),
    [
        (True, None),
        (False, "2026-10-25T16:00:00.123457Z"),
    ],
)
async def test_archived_or_wrong_timestamp_note_remains_uncertain(
    session_factory, archived: bool, timestamp: str | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    provider.note_archived = archived
    provider.note_timestamp_override = timestamp
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.hubspot_note_id is None
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.note_create_count == 1


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (500, None),
        (None, httpx.ReadTimeout("private possible transmission")),
    ],
)
async def test_possible_post_timeout_or_5xx_recovers_without_duplicate_post(
    session_factory, status: int | None, exception: BaseException | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    if status is not None:
        provider.note_status = status
    provider.note_exception = exception
    provider.save_note_before_exception = status == 500
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    first = await _run(session_factory, source, provider)
    second = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)

    assert first.outcome is BookingOutcome.UNCERTAIN
    assert second.replayed is True
    if status == 500:
        assert second.outcome is BookingOutcome.REPLAYED
        assert second.hubspot_note_id == provider.note_id
        assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    else:
        assert second.outcome is BookingOutcome.UNCERTAIN
        assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.note_create_count == 1


async def test_confirmed_rate_limit_allows_only_a_later_explicit_same_key_retry(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    provider.note_status = 429

    first = await _run(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)
    provider.note_status = 201
    second = await _run(session_factory, source, provider)

    assert first.outcome is BookingOutcome.FAILED
    assert first.next_action is BookingOutcomeNextAction.RETRY_SAME_KEY
    assert claim is not None and claim.status is ToolExecutionStatus.FAILED
    assert second.outcome is BookingOutcome.WRITTEN
    assert provider.note_create_count == 2


@pytest.mark.parametrize(
    "response",
    [None, {"properties": {"hs_note_body": "missing id"}}],
)
async def test_malformed_accepted_response_remains_uncertain(
    session_factory, response: object | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    if response is None:
        provider.malformed_note_response = True
    else:
        provider.note_response = response
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert provider.note_create_count == 1
    execution = await _crm_execution(session_factory, source)
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED


async def test_terminal_persistence_failure_keeps_claim_started_and_returns_uncertain(
    session_factory, monkeypatch
) -> None:
    from receptionist.application import crm_booking_outcome as booking_outcome

    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164
    original_terminalize = booking_outcome._terminalize_claim

    def fail_written_terminalization(session, execution, result) -> None:
        if result.outcome is BookingOutcome.WRITTEN:
            raise SQLAlchemyError("private simulated persistence failure")
        original_terminalize(session, execution, result)

    monkeypatch.setattr(booking_outcome, "_terminalize_claim", fail_written_terminalization)

    result = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.hubspot_note_id == provider.note_id
    assert provider.note_create_count == 1
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert execution.sanitized_result is None


async def test_successful_same_source_replay_does_not_post_twice(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    first = await _run(session_factory, source, provider)
    replay = await _run(session_factory, source, provider)

    assert first.outcome is BookingOutcome.WRITTEN
    assert replay.outcome is BookingOutcome.REPLAYED
    assert replay.replayed is True
    assert replay.hubspot_note_id == provider.note_id
    assert provider.note_create_count == 1


async def test_same_source_in_flight_concurrency_cannot_duplicate_post(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_note_post() -> None:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=3)

    provider.before_note_response = hold_note_post
    first_task = asyncio.create_task(_run(session_factory, source, provider))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = await _run(session_factory, source, provider)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=3)

    assert second.outcome is BookingOutcome.UNCERTAIN
    assert second.replayed is True
    assert first.outcome is BookingOutcome.WRITTEN
    assert provider.note_create_count == 1


async def test_different_historical_sources_for_one_booking_produce_distinct_notes(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    reschedule_id = await _add_source_execution(
        session_factory,
        source,
        action="reschedule_booking",
        historical_status=BookingStatus.CONFIRMED,
        finished_at=FINISHED_AT + timedelta(minutes=10),
        historical_start=START_AT + timedelta(hours=1),
        historical_end=END_AT + timedelta(hours=1),
    )
    cancel_id = await _add_source_execution(
        session_factory,
        source,
        action="cancel_booking",
        historical_status=BookingStatus.CANCELLED,
        finished_at=FINISHED_AT + timedelta(minutes=20),
        historical_start=None,
        historical_end=None,
    )
    async with session_factory.begin() as session:
        booking = await session.get(Booking, source.booking_id)
        assert booking is not None
        booking.status = BookingStatus.CANCELLED
        booking.start_at = START_AT + timedelta(hours=3)
        booking.end_at = END_AT + timedelta(hours=3)
        booking.booking_data = {"last_operation": "cancel_booking", "provider_payload": "private"}
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    create_result = await _run(session_factory, source, provider)
    reschedule = Source(
        source.booking_id,
        reschedule_id,
        source.conversation_id,
        source.contact_id,
        source.service_id,
    )
    reschedule_result = await _run(session_factory, reschedule, provider)
    cancel = Source(
        source.booking_id, cancel_id, source.conversation_id, source.contact_id, source.service_id
    )
    cancel_result = await _run(session_factory, cancel, provider)

    bodies = [payload["properties"]["hs_note_body"] for payload in provider.note_payloads]
    assert [create_result.outcome, reschedule_result.outcome, cancel_result.outcome] == [
        BookingOutcome.WRITTEN,
        BookingOutcome.WRITTEN,
        BookingOutcome.WRITTEN,
    ]
    assert (
        len(
            {
                payload["properties"]["hs_note_body"].splitlines()[-1]
                for payload in provider.note_payloads
            }
        )
        == 3
    )
    assert bodies[0].startswith("Booking operation: create_booking\nOutcome: confirmed")
    assert bodies[1].startswith("Booking operation: reschedule_booking\nOutcome: confirmed")
    assert bodies[2].startswith("Booking operation: cancel_booking\nOutcome: cancelled")
    assert provider.note_payloads[1]["properties"]["hs_timestamp"] == (
        "2026-10-25T16:10:00.123456Z"
    )


async def test_older_create_remains_eligible_when_invoked_after_cancellation(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    await _add_source_execution(
        session_factory,
        source,
        action="cancel_booking",
        historical_status=BookingStatus.CANCELLED,
        finished_at=FINISHED_AT + timedelta(minutes=20),
        historical_start=None,
        historical_end=None,
    )
    async with session_factory.begin() as session:
        booking = await session.get(Booking, source.booking_id)
        assert booking is not None
        booking.status = BookingStatus.CANCELLED
        booking.booking_data = {"last_operation": "cancel_booking"}
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.WRITTEN
    assert provider.note_payloads[0]["properties"]["hs_note_body"].startswith(
        "Booking operation: create_booking\nOutcome: confirmed"
    )


@pytest.mark.parametrize(
    "tool_name", ["create_crm_contact", "update_crm_contact", "record_booking_outcome"]
)
async def test_unresolved_earlier_crm_mutation_blocks_new_note(
    session_factory, tool_name: str
) -> None:
    source = await _create_source(session_factory)
    async with session_factory.begin() as session:
        session.add(
            ToolExecution(
                conversation_id=source.conversation_id,
                tool_name=tool_name,
                status=ToolExecutionStatus.STARTED,
                idempotency_key=f"unresolved-update:{uuid.uuid4()}",
                sanitized_arguments={"contact_id": str(source.contact_id)},
            )
        )
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.OPERATION_IN_PROGRESS
    assert result.next_action is BookingOutcomeNextAction.RETRY_SAME_KEY
    assert provider.note_create_count == 0
    assert await _crm_execution(session_factory, source) is None


async def test_claim_and_audit_exclude_pii_and_provider_payloads(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.entity_identifier == str(source.booking_id))
            )
        ).all()
    durable = json.dumps(
        {
            "arguments": None if execution is None else execution.sanitized_arguments,
            "result": None if execution is None else execution.sanitized_result,
            "events": [event.sanitized_metadata for event in events],
        }
    )
    content = json.dumps(provider.note_payloads)

    assert result.outcome is BookingOutcome.WRITTEN
    for private_value in (
        CUSTOMER_NAME,
        CUSTOMER_EMAIL,
        TRANSCRIPT,
        PROVIDER_PAYLOAD,
        AUTH_SENTINEL,
    ):
        assert private_value not in durable
        assert private_value not in content
    assert execution is not None
    assert execution.status is ToolExecutionStatus.SUCCEEDED


async def test_note_write_does_not_modify_booking_calendar_or_source_execution(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        booking_before = await session.get(Booking, source.booking_id)
        source_before = await session.get(ToolExecution, source.source_id)
        assert contact is not None and booking_before is not None and source_before is not None
        before = (
            booking_before.status,
            booking_before.start_at,
            booking_before.end_at,
            booking_before.calendar_id,
            booking_before.calendar_event_id,
            booking_before.booking_data,
            source_before.status,
            source_before.finished_at,
            source_before.sanitized_result,
        )
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    async with session_factory() as session:
        booking_after = await session.get(Booking, source.booking_id)
        source_after = await session.get(ToolExecution, source.source_id)
        assert booking_after is not None and source_after is not None
        after = (
            booking_after.status,
            booking_after.start_at,
            booking_after.end_at,
            booking_after.calendar_id,
            booking_after.calendar_event_id,
            booking_after.booking_data,
            source_after.status,
            source_after.finished_at,
            source_after.sanitized_result,
        )

    assert result.outcome is BookingOutcome.WRITTEN
    assert after == before
    assert await _crm_execution(session_factory, source) is not None


async def test_claim_insert_failure_prevents_note_post(session_factory) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164
    engine = session_factory.kw["bind"].sync_engine

    def fail_claim(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if "insert into tool_execution" in statement.lower():
            raise OperationalError("private injected claim failure", {}, RuntimeError())

    event.listen(engine, "before_cursor_execute", fail_claim)
    try:
        result = await _run(session_factory, source, provider)
    finally:
        event.remove(engine, "before_cursor_execute", fail_claim)

    assert result.outcome is BookingOutcome.FAILED
    assert result.error_code is BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE
    assert provider.note_create_count == 0


async def test_fully_verified_post_skips_reconciliation_and_association_repair(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    async with session_factory() as session:
        contact = await session.get(Contact, source.contact_id)
        assert contact is not None
        props = provider.contact_body["properties"]
        assert isinstance(props, dict)
        props["phone"] = contact.phone_e164

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.WRITTEN
    assert all("/notes/search" not in request.url.path for request in provider.requests)
    assert all("/associations/contact/" not in request.url.path for request in provider.requests)


async def test_real_booking_history_notes_replay_out_of_order_without_calendar_mutation(
    session_factory,
) -> None:
    now_create = datetime(2026, 10, 11, 3, tzinfo=UTC)
    now_reschedule = now_create + timedelta(hours=1)
    now_cancel = now_create + timedelta(hours=2)
    first_start = datetime(2026, 10, 11, 5, 30, tzinfo=UTC)
    first_end = datetime(2026, 10, 11, 6, 30, tzinfo=UTC)
    second_start = first_start + timedelta(days=1)
    second_end = first_end + timedelta(days=1)
    calendar = DeterministicCalendarDouble(calendar_id="crm-history-calendar")

    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        service = await session.scalar(select(Service).where(Service.code == "plumbing"))
        assert service is not None
        service.active = True
        service.bookable = True
        service.booking_requirements = []
        contact = Contact(phone_e164=f"+96650{uuid.uuid4().int % 10_000_000:07d}")
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
        contact_id = contact.id
        conversation_id = conversation.id

    create_preparation = await prepare_create_booking(
        session_factory,
        calendar,
        CreateBookingRequest(
            conversation_id=conversation_id,
            service_selector="plumbing",
            service_area_selector="al_olaya",
            requested_start_at=first_start,
            requested_end_at=first_end,
            idempotency_key=f"history-prepare-create-{uuid.uuid4()}",
        ),
        now_utc=now_create,
    )
    assert isinstance(create_preparation, PreparationResult)
    create_key = f"history-confirm-create-{uuid.uuid4()}"
    create_result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CREATE_BOOKING,
            action_token=create_preparation.data.action_token,
            idempotency_key=create_key,
        ),
        now_utc=now_create,
    )
    assert isinstance(create_result, ConfirmationResult)
    booking_id = create_result.data.booking_id

    reschedule_preparation = await prepare_reschedule_booking(
        session_factory,
        calendar,
        RescheduleBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            requested_start_at=second_start,
            idempotency_key=f"history-prepare-reschedule-{uuid.uuid4()}",
        ),
        now_utc=now_reschedule,
    )
    assert isinstance(reschedule_preparation, PreparationResult)
    reschedule_key = f"history-confirm-reschedule-{uuid.uuid4()}"
    reschedule_result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.RESCHEDULE_BOOKING,
            action_token=reschedule_preparation.data.action_token,
            idempotency_key=reschedule_key,
        ),
        now_utc=now_reschedule,
    )
    assert isinstance(reschedule_result, ConfirmationResult)

    cancel_preparation = await prepare_cancel_booking(
        session_factory,
        CancelBookingRequest(
            conversation_id=conversation_id,
            booking_id=booking_id,
            idempotency_key=f"history-prepare-cancel-{uuid.uuid4()}",
        ),
    )
    assert isinstance(cancel_preparation, PreparationResult)
    cancel_key = f"history-confirm-cancel-{uuid.uuid4()}"
    cancel_result = await confirm_booking_action(
        session_factory,
        calendar,
        ConfirmBookingActionRequest(
            conversation_id=conversation_id,
            action_type=PendingActionType.CANCEL_BOOKING,
            action_token=cancel_preparation.data.action_token,
            idempotency_key=cancel_key,
        ),
        now_utc=now_cancel,
    )
    assert isinstance(cancel_result, ConfirmationResult)
    assert create_result.data.status is BookingStatus.CONFIRMED
    assert reschedule_result.data.status is BookingStatus.CONFIRMED
    assert cancel_result.data.status is BookingStatus.CANCELLED
    assert calendar.mutation_calls == [
        ("create", "crm-history-calendar", create_result.data.calendar_event_id),
        ("patch", "crm-history-calendar", create_result.data.calendar_event_id),
        ("cancel", "crm-history-calendar", create_result.data.calendar_event_id),
    ]

    source_by_action: dict[str, Source] = {}
    for action, confirmation_key in (
        ("create_booking", create_key),
        ("reschedule_booking", reschedule_key),
        ("cancel_booking", cancel_key),
    ):
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecution).where(ToolExecution.idempotency_key == confirmation_key)
            )
            contact = await session.get(Contact, contact_id)
            booking = await session.get(Booking, booking_id)
            assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
            assert execution.finished_at is not None
            assert contact is not None and booking is not None
            source_by_action[action] = Source(
                booking_id=booking_id,
                source_id=execution.id,
                conversation_id=conversation_id,
                contact_id=contact_id,
                service_id=booking.service_id,
            )
            local_phone = contact.phone_e164
    assert local_phone is not None

    provider = MockHubSpot(email=None, phone=local_phone)
    async with session_factory.begin() as session:
        session.add(
            CRMContactLink(
                contact_id=contact_id,
                hubspot_portal_id=PORTAL_ID,
                hubspot_contact_id=provider.contact_id,
                linked_by=CRMContactLinkProvenance.PHONE_E164,
                state=CRMContactLinkState.ACTIVE,
            )
        )

    # A HubSpot outage cannot roll back or reverse the completed Calendar cancellation.
    provider.account_status = 503
    unavailable = await _run(session_factory, source_by_action["cancel_booking"], provider)
    assert unavailable.outcome is BookingOutcome.FAILED
    assert provider.note_create_count == 0
    async with session_factory() as session:
        current_booking = await session.get(Booking, booking_id)
        assert current_booking is not None
        assert current_booking.status is BookingStatus.CANCELLED
        assert current_booking.start_at == second_start
        assert current_booking.end_at == second_end
    assert (
        await calendar.get_event("crm-history-calendar", create_result.data.calendar_event_id)
        is None
    )
    provider.account_status = 200

    # Delivery order differs from booking order; every source retains its own history.
    delivery_order = ("reschedule_booking", "create_booking", "cancel_booking")
    delivered: dict[str, object] = {}
    for action in delivery_order:
        source = source_by_action[action]
        result = await _run(session_factory, source, provider)
        assert result.outcome is BookingOutcome.WRITTEN
        delivered[action] = result

    expected_slots = {
        "create_booking": (first_start, first_end, "confirmed"),
        "reschedule_booking": (second_start, second_end, "confirmed"),
        "cancel_booking": (second_start, second_end, "cancelled"),
    }
    for action, source in source_by_action.items():
        result = delivered[action]
        note = provider.saved_notes[result.hubspot_note_id]
        properties = note["properties"]
        async with session_factory() as session:
            execution = await session.get(ToolExecution, source.source_id)
            assert execution is not None and execution.finished_at is not None
            historical = ConfirmationResult.model_validate(execution.sanitized_result)
        start_at, end_at, status = expected_slots[action]
        start_text = start_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        end_text = end_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        expected_body = (
            f"Booking operation: {action}\n"
            f"Outcome: {status}\n"
            f"Booking reference: {booking_id}\n"
            f"Requested start (UTC): {start_text}\n"
            f"Requested end (UTC): {end_text}\n"
            f"Source operation: booking-execution:{source.source_id}"
        )
        assert historical.data.booking_id == booking_id
        assert historical.data.requested_start_at_utc == start_at
        assert historical.data.requested_end_at_utc == end_at
        assert properties["hs_note_body"] == expected_body
        assert properties["hs_timestamp"] == (
            execution.finished_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    assert len(provider.saved_notes) == 3
    first_write_count = provider.note_create_count
    for action in delivery_order:
        replay = await _run(session_factory, source_by_action[action], provider)
        assert replay.outcome is BookingOutcome.REPLAYED
        assert replay.replayed is True
    assert provider.note_create_count == first_write_count == 3
    assert calendar.mutation_calls == [
        ("create", "crm-history-calendar", create_result.data.calendar_event_id),
        ("patch", "crm-history-calendar", create_result.data.calendar_event_id),
        ("cancel", "crm-history-calendar", create_result.data.calendar_event_id),
    ]
    async with session_factory() as session:
        current_booking = await session.get(Booking, booking_id)
        assert current_booking is not None
        assert current_booking.status is BookingStatus.CANCELLED
        assert current_booking.start_at == second_start
        assert current_booking.end_at == second_end


@pytest.mark.parametrize(
    ("failure", "save_note"),
    [
        (httpx.ReadTimeout("private timeout"), True),
        (None, False),
    ],
)
async def test_same_key_reconciles_possible_note_without_second_post(
    session_factory, failure: BaseException | None, save_note: bool
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    if failure is None:
        provider.note_status = 500
    await _leave_possible_note(
        session_factory,
        source,
        provider,
        saved_before_failure=save_note,
        failure=failure,
    )

    if save_note:
        result = await _run(session_factory, source, provider)
        assert result.outcome is BookingOutcome.REPLAYED
        assert result.hubspot_note_id == provider.note_id
        assert provider.note_create_count == 1
        assert provider.note_search_count == 1
        execution = await _crm_execution(session_factory, source)
        assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    else:
        result = await _run(session_factory, source, provider)
        assert result.outcome is BookingOutcome.UNCERTAIN
        assert result.next_action is BookingOutcomeNextAction.RECONCILE_SAME_KEY
        assert provider.note_create_count == 1


async def test_delayed_note_indexing_stays_uncertain_then_recovers(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    provider.search_visible_after = 1

    first_recovery = await _run(session_factory, source, provider)
    second_recovery = await _run(session_factory, source, provider)

    assert first_recovery.outcome is BookingOutcome.UNCERTAIN
    assert first_recovery.next_action is BookingOutcomeNextAction.RECONCILE_SAME_KEY
    assert second_recovery.outcome is BookingOutcome.REPLAYED
    assert provider.note_create_count == 1
    assert provider.note_search_count == 2


@pytest.mark.parametrize(
    "body_change",
    [
        "unrelated booking-execution marker substring",
        "append an unrelated line",
    ],
)
async def test_reconciliation_requires_exact_marker_and_canonical_body(
    session_factory, body_change: str
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    properties = provider.saved_note["properties"]
    assert isinstance(properties, dict)
    if body_change == "unrelated booking-execution marker substring":
        properties["hs_note_body"] = f"prefix booking-execution:{source.source_id} suffix"
    else:
        properties["hs_note_body"] = f"{properties['hs_note_body']}\nUnapproved extra text"

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.hubspot_note_id is None
    assert provider.note_create_count == 1


@pytest.mark.parametrize(
    ("archived", "timestamp"),
    [(True, None), (False, "2026-10-25T16:00:00.123457Z")],
)
async def test_reconciliation_rejects_archived_or_wrong_timestamp(
    session_factory, archived: bool, timestamp: str | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note["archived"] = archived
    if timestamp is not None:
        properties = provider.saved_note["properties"]
        assert isinstance(properties, dict)
        properties["hs_timestamp"] = timestamp

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert provider.note_create_count == 1
    assert provider.association_put_count == 0


async def test_reconciliation_rejects_duplicate_exact_marker_notes(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    duplicate_id = str(int(provider.note_id) + 1)
    duplicate = dict(provider.saved_note)
    duplicate["id"] = duplicate_id
    provider.saved_notes[duplicate_id] = duplicate

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.error_code is BookingOutcomeErrorCode.NOTE_SEARCH_AMBIGUOUS
    assert result.next_action is BookingOutcomeNextAction.OPERATOR_REVIEW
    assert provider.note_create_count == 1
    assert provider.association_put_count == 0


async def test_reconciliation_does_not_select_first_incomplete_search_page(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    provider.search_incomplete = True

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.error_code is BookingOutcomeErrorCode.NOTE_SEARCH_INCOMPLETE
    assert provider.note_read_count == 0
    assert provider.note_create_count == 1


@pytest.mark.parametrize(
    ("status", "exception"),
    [(429, None), (503, None), (None, httpx.ReadTimeout("private search timeout"))],
)
async def test_search_provider_failures_preserve_uncertain_claim(
    session_factory, status: int | None, exception: BaseException | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    provider.search_status = status or 200
    provider.search_exception = exception

    result = await _run(session_factory, source, provider)
    execution = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.replayed is True
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.note_create_count == 1


async def test_accepted_malformed_response_is_reconciled_by_same_key(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    provider.malformed_note_response = True

    first = await _run(session_factory, source, provider)
    provider.malformed_note_response = False
    second = await _run(session_factory, source, provider)

    assert first.outcome is BookingOutcome.UNCERTAIN
    assert second.outcome is BookingOutcome.REPLAYED
    assert second.hubspot_note_id == provider.note_id
    assert provider.note_create_count == 1


async def test_note_created_before_local_terminalization_failure_is_reconciled(
    session_factory, monkeypatch
) -> None:
    from receptionist.application import crm_booking_outcome as booking_outcome

    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    original_terminalize = booking_outcome._terminalize_claim

    def fail_first_terminalization(session, execution, result) -> None:
        if result.outcome is BookingOutcome.WRITTEN:
            raise SQLAlchemyError("private terminal write failure")
        original_terminalize(session, execution, result)

    monkeypatch.setattr(booking_outcome, "_terminalize_claim", fail_first_terminalization)
    first = await _run(session_factory, source, provider)
    monkeypatch.setattr(booking_outcome, "_terminalize_claim", original_terminalize)
    second = await _run(session_factory, source, provider)

    assert first.outcome is BookingOutcome.UNCERTAIN
    assert first.hubspot_note_id == provider.note_id
    assert second.outcome is BookingOutcome.REPLAYED
    assert provider.note_create_count == 1


async def test_reconciliation_ignores_candidate_with_different_exact_marker(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider, saved_before_failure=False)
    other_id = str(int(provider.note_id) + 1)
    provider.saved_notes[other_id] = {
        "id": other_id,
        "properties": {
            "hs_note_body": f"Source operation: booking-execution:{source.source_id}-other",
            "hs_timestamp": "2026-10-25T16:00:00.123456Z",
        },
        "archived": False,
        "associations": {"contacts": {"results": [{"id": provider.contact_id}]}},
    }

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.error_code is BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED
    assert provider.note_create_count == 1


async def test_invalid_search_note_id_preserves_uncertainty(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider, saved_before_failure=False)
    provider.search_response = {
        "total": 1,
        "results": [
            {
                "id": "invalid-note-id",
                "properties": {"hs_note_body": "invalid", "hs_timestamp": "now"},
            }
        ],
    }

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert provider.note_create_count == 1
    assert provider.note_read_count == 0


async def test_provider_note_read_failure_keeps_durable_claim_uncertain(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    provider.note_read_status = 503

    result = await _run(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert claim is not None and claim.status is ToolExecutionStatus.STARTED
    assert provider.note_create_count == 1
    assert provider.association_put_count == 0


async def test_association_put_timeout_is_read_back_before_replay(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note["associations"] = {"contacts": {"results": []}}
    provider.association_put_exception = httpx.ReadTimeout("private association timeout")
    provider.association_put_after_mutation = True

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.REPLAYED
    assert provider.association_put_count == 1
    assert provider.note_create_count == 1


async def test_claim_portal_binding_mismatch_is_not_reconciled(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)
    assert claim is not None
    async with session_factory.begin() as session:
        current = await session.get(ToolExecution, claim.id)
        assert current is not None
        assert current.sanitized_arguments is not None
        current.sanitized_arguments = {
            **current.sanitized_arguments,
            "hubspot_portal_id": "654321",
        }

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.error_code is BookingOutcomeErrorCode.CLAIM_BINDING_MISMATCH
    assert result.next_action is BookingOutcomeNextAction.OPERATOR_REVIEW
    assert provider.note_search_count == 0
    assert provider.note_create_count == 1


async def test_account_preflight_failure_does_not_search_or_repair(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    provider.portal_id = 654321

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.error_code is BookingOutcomeErrorCode.ACCOUNT_MISMATCH
    assert result.next_action is BookingOutcomeNextAction.OPERATOR_REVIEW
    assert provider.note_search_count == 0
    assert provider.association_put_count == 0
    assert provider.note_create_count == 1


@pytest.mark.parametrize(
    ("association_kind", "association_ids"),
    [
        ("wrong", ["other-contact"]),
        ("multiple", ["expected-contact", "other-contact"]),
        ("malformed", None),
        ("missing", None),
    ],
)
async def test_reconciliation_never_repairs_conflicting_or_incomplete_associations(
    session_factory, association_kind: str, association_ids: list[str] | None
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    if association_kind == "missing":
        provider.saved_note.pop("associations", None)
    elif association_kind == "malformed":
        provider.saved_note["associations"] = {"contacts": {"results": "malformed"}}
    else:
        assert association_ids is not None
        provider.saved_note["associations"] = {
            "contacts": {
                "results": [
                    {"id": provider.contact_id if value == "expected-contact" else value}
                    for value in association_ids
                ]
            }
        }

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert provider.association_put_count == 0
    assert provider.note_create_count == 1


async def test_verified_empty_association_is_narrowly_repaired(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note["associations"] = {"contacts": {"results": []}}

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.REPLAYED
    assert result.hubspot_note_id == provider.note_id
    assert provider.association_put_count == 1
    assert provider.note_create_count == 1
    assert provider.saved_note["associations"] == {
        "contacts": {"results": [{"id": provider.contact_id}]}
    }


async def test_repair_readback_failure_is_reconciled_without_second_put(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note["associations"] = {"contacts": {"results": []}}
    provider.note_read_fail_on_count = 3

    first_recovery = await _run(session_factory, source, provider)
    provider.note_read_fail_on_count = None
    second_recovery = await _run(session_factory, source, provider)

    assert first_recovery.outcome is BookingOutcome.UNCERTAIN
    assert second_recovery.outcome is BookingOutcome.REPLAYED
    assert provider.association_put_count == 1
    assert provider.note_create_count == 1


async def test_unavailable_association_metadata_is_not_treated_as_empty(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note.pop("associations", None)

    result = await _run(session_factory, source, provider)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert provider.association_put_count == 0
    assert provider.note_create_count == 1


async def test_concurrent_recovery_serializes_same_key_and_association_repair(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    assert provider.saved_note is not None
    provider.saved_note["associations"] = {"contacts": {"results": []}}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_association_put() -> None:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=3)

    provider.before_association_put = hold_association_put
    first_task = asyncio.create_task(_run(session_factory, source, provider))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = await _run(session_factory, source, provider)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=3)

    assert first.outcome is BookingOutcome.REPLAYED
    assert second.outcome is BookingOutcome.UNCERTAIN
    assert second.error_code is BookingOutcomeErrorCode.OPERATION_IN_PROGRESS
    assert provider.association_put_count == 1
    assert provider.note_create_count == 1


async def test_changed_mapping_or_source_binding_keeps_uncertain_claim(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    async with session_factory.begin() as session:
        link = await session.get(CRMContactLink, source.contact_id)
        assert link is not None
        link.hubspot_contact_id = str(int(link.hubspot_contact_id) + 1)

    changed_mapping = await _run(session_factory, source, provider)
    assert changed_mapping.outcome is BookingOutcome.UNCERTAIN
    assert changed_mapping.error_code is BookingOutcomeErrorCode.CLAIM_BINDING_MISMATCH
    assert provider.note_search_count == 0
    assert provider.note_create_count == 1


async def test_changed_historical_source_keeps_claim_uncertain_without_search(
    session_factory,
) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    async with session_factory.begin() as session:
        execution = await session.get(ToolExecution, source.source_id)
        assert execution is not None
        execution.status = ToolExecutionStatus.FAILED

    result = await _run(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)

    assert result.outcome is BookingOutcome.UNCERTAIN
    assert result.next_action is BookingOutcomeNextAction.OPERATOR_REVIEW
    assert provider.note_search_count == 0
    assert provider.note_create_count == 1
    assert claim is not None and claim.status is ToolExecutionStatus.STARTED


async def test_recovery_local_terminalization_failure_is_safe_and_replayable(
    session_factory, monkeypatch
) -> None:
    from receptionist.application import crm_booking_outcome as booking_outcome

    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    original_terminalize = booking_outcome._terminalize_claim

    def fail_recovery_terminalization(session, execution, result) -> None:
        if result.outcome is BookingOutcome.REPLAYED:
            raise SQLAlchemyError("private recovery persistence failure")
        original_terminalize(session, execution, result)

    monkeypatch.setattr(booking_outcome, "_terminalize_claim", fail_recovery_terminalization)
    first = await _run(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)
    monkeypatch.setattr(booking_outcome, "_terminalize_claim", original_terminalize)
    second = await _run(session_factory, source, provider)

    assert first.outcome is BookingOutcome.UNCERTAIN
    assert first.error_code is BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE
    assert claim is not None and claim.status is ToolExecutionStatus.STARTED
    assert second.outcome is BookingOutcome.REPLAYED
    assert provider.note_create_count == 1


async def test_recovery_never_calls_calendar_and_terminalizes_one_claim(session_factory) -> None:
    source = await _create_source(session_factory)
    provider = MockHubSpot(email=None, phone=None)
    await _bind_provider_identity(session_factory, source, provider)
    await _leave_possible_note(session_factory, source, provider)
    result = await _run(session_factory, source, provider)
    claim = await _crm_execution(session_factory, source)
    async with session_factory() as session:
        executions = (
            await session.scalars(
                select(ToolExecution).where(
                    ToolExecution.idempotency_key.is_not(None),
                    ToolExecution.tool_name == "record_booking_outcome",
                )
            )
        ).all()

    assert result.outcome is BookingOutcome.REPLAYED
    assert claim is not None and claim.status is ToolExecutionStatus.SUCCEEDED
    assert claim.finished_at is not None
    assert [execution.id for execution in executions].count(claim.id) == 1
    assert provider.note_create_count == 1
    assert all("calendar" not in request.url.host for request in provider.requests)

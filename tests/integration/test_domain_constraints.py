from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from receptionist.db.models import (
    AuditEvent,
    Booking,
    BusinessConfig,
    Contact,
    Conversation,
    ConversationTurn,
    Handoff,
    OutboxEvent,
    ProviderEventReceipt,
    Service,
    ToolExecution,
)
from receptionist.domain.enums import (
    AuditActorType,
    BookingStatus,
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
    ConversationTurnRole,
    HandoffPriority,
    HandoffReason,
    HandoffStatus,
    ToolExecutionStatus,
)
from receptionist.seed import build_reference_business, build_reference_services

pytestmark = pytest.mark.integration


def _business() -> BusinessConfig:
    return BusinessConfig(id=1, **build_reference_business().model_dump(mode="json"))


def _service(code: str | None = None) -> Service:
    values = build_reference_services()[0].model_dump(mode="json")
    values["code"] = code or f"constraint-test-{uuid.uuid4().hex}"
    return Service(**values)


async def _create_contact_conversation_service(session_factory):
    async with session_factory.begin() as session:
        contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
        service = _service()
        session.add_all([contact, service])
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
        return contact.id, service.id, conversation.id


async def test_business_singleton_and_contact_null_identity_rules(session_factory) -> None:
    async with session_factory.begin() as session:
        existing = await session.get(BusinessConfig, 1)
        if existing is None:
            session.add(_business())

    async with session_factory() as session:
        invalid = _business()
        invalid.id = 2
        session.add(invalid)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    phone = f"+1999{uuid.uuid4().int % 10**10:010d}"
    async with session_factory.begin() as session:
        session.add_all(
            [Contact(phone_e164=phone), Contact(phone_e164=None), Contact(phone_e164=None)]
        )

    async with session_factory() as session:
        session.add(Contact(phone_e164=phone))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_invalid_conversation_contact_foreign_key_is_rejected(session_factory) -> None:
    async with session_factory() as session:
        session.add(
            Conversation(
                contact_id=uuid.uuid4(),
                channel=ConversationChannel.PHONE,
                status=ConversationStatus.OPEN,
                control_mode=ControlMode.AI,
                language_mode=ConversationLanguageMode.UNKNOWN,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_conversation_turn_sequence_is_unique_per_conversation(session_factory) -> None:
    _, _, conversation_id = await _create_contact_conversation_service(session_factory)
    async with session_factory.begin() as session:
        contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
        other_conversation = Conversation(
            contact_id=contact.id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(contact)
        await session.flush()
        other_conversation.contact_id = contact.id
        session.add(other_conversation)
        await session.flush()
        other_conversation_id = other_conversation.id

    async with session_factory() as session:
        session.add_all(
            [
                ConversationTurn(
                    conversation_id=conversation_id,
                    sequence_number=1,
                    role=ConversationTurnRole.CUSTOMER,
                    content="first",
                ),
                ConversationTurn(
                    conversation_id=conversation_id,
                    sequence_number=1,
                    role=ConversationTurnRole.ASSISTANT,
                    content="duplicate",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory.begin() as session:
        session.add_all(
            [
                ConversationTurn(
                    conversation_id=conversation_id,
                    sequence_number=1,
                    role=ConversationTurnRole.CUSTOMER,
                    content="first",
                ),
                ConversationTurn(
                    conversation_id=other_conversation_id,
                    sequence_number=1,
                    role=ConversationTurnRole.CUSTOMER,
                    content="first",
                ),
            ]
        )


async def test_booking_time_pair_and_order_constraints(session_factory) -> None:
    contact_id, service_id, _ = await _create_contact_conversation_service(session_factory)
    start_at = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end_at = start_at + timedelta(hours=1)

    async with session_factory() as session:
        session.add(
            Booking(
                contact_id=contact_id,
                service_id=service_id,
                status=BookingStatus.PENDING,
                start_at=start_at,
                booking_data={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory() as session:
        session.add(
            Booking(
                contact_id=contact_id,
                service_id=service_id,
                status=BookingStatus.PENDING,
                start_at=end_at,
                end_at=start_at,
                booking_data={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory.begin() as session:
        session.add_all(
            [
                Booking(
                    contact_id=contact_id,
                    service_id=service_id,
                    status=BookingStatus.PENDING,
                    booking_data={},
                ),
                Booking(
                    contact_id=contact_id,
                    service_id=service_id,
                    status=BookingStatus.PENDING,
                    start_at=start_at,
                    end_at=end_at,
                    booking_data={},
                ),
            ]
        )


async def test_handoff_active_partial_uniqueness_allows_history(session_factory) -> None:
    _, _, conversation_id = await _create_contact_conversation_service(session_factory)

    async with session_factory() as session:
        session.add_all(
            [
                Handoff(
                    conversation_id=conversation_id,
                    status=HandoffStatus.PENDING,
                    reason=HandoffReason.EXPLICIT_REQUEST,
                    priority=HandoffPriority.NORMAL,
                ),
                Handoff(
                    conversation_id=conversation_id,
                    status=HandoffStatus.ACCEPTED,
                    reason=HandoffReason.LOW_CONFIDENCE,
                    priority=HandoffPriority.HIGH,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory.begin() as session:
        session.add_all(
            [
                Handoff(
                    conversation_id=conversation_id,
                    status=HandoffStatus.RESOLVED,
                    reason=HandoffReason.EXPLICIT_REQUEST,
                    priority=HandoffPriority.NORMAL,
                ),
                Handoff(
                    conversation_id=conversation_id,
                    status=HandoffStatus.CANCELLED,
                    reason=HandoffReason.LOW_CONFIDENCE,
                    priority=HandoffPriority.NORMAL,
                ),
                Handoff(
                    conversation_id=conversation_id,
                    status=HandoffStatus.PENDING,
                    reason=HandoffReason.UNKNOWN_INFORMATION,
                    priority=HandoffPriority.URGENT,
                ),
            ]
        )


async def test_tool_idempotency_and_provider_receipt_uniqueness(session_factory) -> None:
    _, _, conversation_id = await _create_contact_conversation_service(session_factory)
    external_event_id = f"same-event-{uuid.uuid4().hex}"

    async with session_factory() as session:
        session.add_all(
            [
                ToolExecution(
                    conversation_id=conversation_id,
                    tool_name="phase1_demo",
                    status=ToolExecutionStatus.STARTED,
                    idempotency_key="same-key",
                    sanitized_arguments={},
                ),
                ToolExecution(
                    conversation_id=conversation_id,
                    tool_name="phase1_demo",
                    status=ToolExecutionStatus.STARTED,
                    idempotency_key="same-key",
                    sanitized_arguments={},
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory.begin() as session:
        session.add_all(
            [
                ToolExecution(
                    conversation_id=conversation_id,
                    tool_name="phase1_demo",
                    status=ToolExecutionStatus.STARTED,
                    sanitized_arguments={},
                ),
                ToolExecution(
                    conversation_id=conversation_id,
                    tool_name="phase1_demo",
                    status=ToolExecutionStatus.STARTED,
                    sanitized_arguments={},
                ),
            ]
        )

    async with session_factory() as session:
        session.add_all(
            [
                ProviderEventReceipt(provider="provider_a", external_event_id=external_event_id),
                ProviderEventReceipt(provider="provider_a", external_event_id=external_event_id),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async with session_factory.begin() as session:
        session.add(
            ProviderEventReceipt(provider="provider_b", external_event_id=external_event_id)
        )


async def test_database_rejects_invalid_controlled_strings(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO contact (id, preferred_language) "
                    "VALUES (:id, 'xx')"
                ),
                {"id": uuid.uuid4()},
            )
        await session.rollback()


async def test_phase_one_event_records_store_sanitized_payloads(session_factory) -> None:
    contact_id, _, conversation_id = await _create_contact_conversation_service(session_factory)

    async with session_factory.begin() as session:
        session.add_all(
            [
                AuditEvent(
                    event_type="phase1.test",
                    actor_type=AuditActorType.SYSTEM,
                    contact_id=contact_id,
                    conversation_id=conversation_id,
                    sanitized_metadata={"safe": True},
                ),
                OutboxEvent(event_type="phase1.test", payload={"safe": True}),
            ]
        )

    async with session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.conversation_id == conversation_id)
        )
        outbox = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "phase1.test")
        )
        assert audit is not None
        assert audit.sanitized_metadata == {"safe": True}
        assert outbox is not None
        assert outbox.published_at is None

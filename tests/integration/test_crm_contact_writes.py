from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from receptionist.application.crm_contact_writes import (
    CRMContactCreateRequest,
    CRMContactUpdateRequest,
    CRMContactWriteValidationOutcome,
    validate_crm_contact_write,
)
from receptionist.db.models import AuditEvent, Contact, Conversation, ToolExecution
from receptionist.domain.enums import (
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
)
from receptionist.integrations.hubspot import HubSpotClient

pytestmark = pytest.mark.integration


async def _make_owner(session_factory, *, email: str | None = None):
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    async with session_factory.begin() as session:
        contact = Contact(id=contact_id, phone_e164=phone, email=email)
        session.add(contact)
        await session.flush()
        session.add(
            Conversation(
                id=conversation_id,
                contact_id=contact_id,
                channel=ConversationChannel.PHONE,
                status=ConversationStatus.OPEN,
                control_mode=ControlMode.AI,
                language_mode=ConversationLanguageMode.UNKNOWN,
            )
        )
    return contact_id, conversation_id, phone


def _ids(contact_id: uuid.UUID, conversation_id: uuid.UUID) -> dict[str, object]:
    return {
        "contact_id": contact_id,
        "conversation_id": conversation_id,
        "idempotency_key": str(uuid.uuid4()),
    }


async def test_valid_create_ownership_check_is_read_only_and_provider_free(
    session_factory, monkeypatch
) -> None:
    contact_id, conversation_id, phone = await _make_owner(session_factory)
    email = "trusted-create@example.test"
    request = CRMContactCreateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_email=email,
        trusted_verified_phone=phone,
    )

    def reject_provider_client(*_args, **_kwargs):
        raise AssertionError("Task 8 validation must not construct a HubSpot client")

    monkeypatch.setattr(HubSpotClient, "__init__", reject_provider_client)
    async with session_factory() as session:
        executions_before = await session.scalar(select(func.count()).select_from(ToolExecution))
        audits_before = await session.scalar(select(func.count()).select_from(AuditEvent))

    result = await validate_crm_contact_write(session_factory, request)

    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        executions_after = await session.scalar(select(func.count()).select_from(ToolExecution))
        audits_after = await session.scalar(select(func.count()).select_from(AuditEvent))
    assert result.outcome is CRMContactWriteValidationOutcome.VALID
    assert contact is not None and contact.email is None and contact.phone_e164 == phone
    assert executions_after == executions_before
    assert audits_after == audits_before


async def test_valid_update_can_use_email_or_phone_only(session_factory) -> None:
    contact_id, conversation_id, phone = await _make_owner(session_factory)
    email_request = CRMContactUpdateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_email="trusted-update@example.test",
    )
    phone_request = CRMContactUpdateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_phone=phone,
    )

    email_result = await validate_crm_contact_write(session_factory, email_request)
    phone_result = await validate_crm_contact_write(session_factory, phone_request)

    assert email_result.outcome is CRMContactWriteValidationOutcome.VALID
    assert phone_result.outcome is CRMContactWriteValidationOutcome.VALID


async def test_cross_contact_conversation_is_rejected(session_factory) -> None:
    first_contact, conversation_id, _ = await _make_owner(session_factory)
    second_contact, _second_conversation, _ = await _make_owner(session_factory)
    request = CRMContactCreateRequest(
        **_ids(second_contact, conversation_id),
        trusted_verified_email="cross-owner@example.test",
    )

    result = await validate_crm_contact_write(session_factory, request)

    assert result.outcome is CRMContactWriteValidationOutcome.OWNERSHIP_MISMATCH
    assert result.error_code is not None
    assert result.operation == "create_crm_contact"
    assert first_contact != second_contact


async def test_missing_contact_is_a_safe_invalid_request(session_factory) -> None:
    contact_id, conversation_id, _ = await _make_owner(session_factory)
    request = CRMContactCreateRequest(
        **_ids(uuid.uuid4(), conversation_id),
        trusted_verified_email="missing-contact@example.test",
    )

    result = await validate_crm_contact_write(session_factory, request)

    assert result.outcome is CRMContactWriteValidationOutcome.INVALID_REQUEST
    assert result.error_code is not None
    assert str(contact_id) not in str(result.model_dump(mode="json"))


async def test_missing_conversation_is_a_safe_invalid_request(session_factory) -> None:
    contact_id, _conversation_id, _ = await _make_owner(session_factory)
    request = CRMContactUpdateRequest(
        **_ids(contact_id, uuid.uuid4()),
        trusted_verified_email="missing-conversation@example.test",
    )

    result = await validate_crm_contact_write(session_factory, request)

    assert result.outcome is CRMContactWriteValidationOutcome.INVALID_REQUEST
    assert result.error_code is not None


async def test_conflicting_local_email_is_rejected_without_mutation(session_factory) -> None:
    contact_id, conversation_id, phone = await _make_owner(
        session_factory, email="unverified-existing@example.test"
    )
    request = CRMContactCreateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_email="different-trusted@example.test",
        trusted_verified_phone=phone,
    )

    result = await validate_crm_contact_write(session_factory, request)

    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
    assert result.outcome is CRMContactWriteValidationOutcome.IDENTITY_CONFLICT
    assert contact is not None and contact.email == "unverified-existing@example.test"
    assert "unverified-existing@example.test" not in str(result.model_dump(mode="json"))
    assert "different-trusted@example.test" not in str(result.model_dump(mode="json"))


@pytest.mark.parametrize("existing_email", ["", "   "])
async def test_blank_local_email_does_not_conflict_with_trusted_email(
    session_factory, existing_email: str
) -> None:
    contact_id, conversation_id, _ = await _make_owner(session_factory, email=existing_email)
    request = CRMContactCreateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_email="verified-value@example.test",
    )

    result = await validate_crm_contact_write(session_factory, request)

    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
    assert result.outcome is CRMContactWriteValidationOutcome.VALID
    assert contact is not None and contact.email == existing_email


async def test_mismatching_trusted_phone_is_rejected_without_mutation(session_factory) -> None:
    contact_id, conversation_id, phone = await _make_owner(session_factory)
    request = CRMContactUpdateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_phone="+966551234567",
    )

    result = await validate_crm_contact_write(session_factory, request)

    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
    assert result.outcome is CRMContactWriteValidationOutcome.IDENTITY_CONFLICT
    assert contact is not None and contact.phone_e164 == phone


async def test_unverified_existing_email_is_not_used_for_phone_only_update(session_factory) -> None:
    contact_id, conversation_id, phone = await _make_owner(
        session_factory, email="unverified-existing@example.test"
    )
    request = CRMContactUpdateRequest(
        **_ids(contact_id, conversation_id),
        trusted_verified_phone=phone,
    )

    result = await validate_crm_contact_write(session_factory, request)

    assert result.outcome is CRMContactWriteValidationOutcome.VALID

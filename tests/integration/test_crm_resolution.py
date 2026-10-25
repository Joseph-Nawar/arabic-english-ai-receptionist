from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from receptionist.application.crm_resolution import (
    CRMContactResolutionOutcome,
    CRMContactResolutionRequest,
    CRMResolutionNextAction,
    resolve_crm_contact,
)
from receptionist.core.config import Settings
from receptionist.db.models import (
    AuditEvent,
    Contact,
    Conversation,
    CRMContactLink,
    CRMContactLinkState,
    ToolExecution,
)
from receptionist.domain.enums import (
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
)
from receptionist.domain.identity import CRMContactLinkProvenance
from receptionist.seed import seed_reference_data

pytestmark = pytest.mark.integration

PORTAL_ID = "123456"
AUTH_SENTINEL = "fake-hubspot-service-key"
TEST_DATABASE_URL = "postgresql+psycopg://receptionist@localhost/receptionist_test"
_PROVIDER_RUN_PREFIX = str(uuid.uuid4().int)[:12]


def _provider_id(suffix: str) -> str:
    """Namespace opaque provider IDs per run for repeatable shared-DB tests."""
    return f"{_PROVIDER_RUN_PREFIX}{suffix}"


def _settings(*, portal_id: str = PORTAL_ID) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        hubspot_service_key=AUTH_SENTINEL,
        hubspot_portal_id=portal_id,
        hubspot_request_timeout_seconds=1.0,
    )


def _contact_payload(
    record_id: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    mobilephone: str | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    return {
        "id": record_id,
        "properties": {"email": email, "phone": phone, "mobilephone": mobilephone},
        "archived": archived,
    }


class MockHubSpot:
    def __init__(self) -> None:
        self.account_status = 200
        self.portal_id = int(PORTAL_ID)
        self.account_body: object | None = None
        self.contact_by_id: dict[str, tuple[int, object]] = {}
        self.contact_by_email: dict[str, tuple[int, object]] = {}
        self.search_status = 200
        self.search_body: object = {"results": [], "total": 0}
        self.search_exception: BaseException | None = None
        self.requests: list[tuple[str, str]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/account-info/2026-03/details":
            body = self.account_body or {"portalId": self.portal_id}
            return httpx.Response(self.account_status, json=body)

        if request.url.path.endswith("/contacts/search"):
            if self.search_exception is not None:
                raise self.search_exception
            return httpx.Response(self.search_status, json=self.search_body)

        record_id_or_email = unquote(request.url.path.rsplit("/", maxsplit=1)[-1])
        if request.url.params.get("idProperty") == "email":
            status, body = self.contact_by_email.get(
                record_id_or_email, (404, {"message": "missing"})
            )
            return httpx.Response(status, json=body)

        status, body = self.contact_by_id.get(record_id_or_email, (404, {"message": "missing"}))
        return httpx.Response(status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def _local_identity(
    session_factory,
    *,
    phone: str | None = "generated",
    email: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str | None]:
    if phone == "generated":
        phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    async with session_factory.begin() as session:
        await seed_reference_data(session, "test")
        contact = Contact(phone_e164=phone, email=email)
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
        return contact.id, conversation.id, phone


async def _add_link(
    session_factory,
    contact_id: uuid.UUID,
    provider_id: str,
    provenance: CRMContactLinkProvenance,
    *,
    portal_id: str = PORTAL_ID,
    state: CRMContactLinkState = CRMContactLinkState.ACTIVE,
    verified_at: datetime | None = None,
    email: str | None = None,
) -> None:
    async with session_factory.begin() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        if provenance is CRMContactLinkProvenance.VERIFIED_EMAIL:
            contact.email = email
        session.add(
            CRMContactLink(
                contact_id=contact_id,
                hubspot_portal_id=portal_id,
                hubspot_contact_id=provider_id,
                linked_by=provenance,
                state=state,
                verified_at=verified_at or datetime.now(UTC) - timedelta(days=1),
            )
        )


async def _link(session_factory, contact_id: uuid.UUID) -> CRMContactLink | None:
    async with session_factory() as session:
        return await session.get(CRMContactLink, contact_id)


async def _contact_email(session_factory, contact_id: uuid.UUID) -> str | None:
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        return None if contact is None else contact.email


def _request(
    contact_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    email: str | None = None,
    key: str | None = None,
) -> CRMContactResolutionRequest:
    return CRMContactResolutionRequest(
        conversation_id=conversation_id,
        contact_id=contact_id,
        idempotency_key=key or f"resolve-{uuid.uuid4().hex}",
        trusted_verified_email=email,
    )


async def _resolve(
    session_factory,
    request: CRMContactResolutionRequest,
    provider: MockHubSpot,
    *,
    portal_id: str = PORTAL_ID,
):
    return await resolve_crm_contact(
        session_factory,
        request,
        _settings(portal_id=portal_id),
        transport=provider.transport(),
    )


def _search(
    *contacts: dict[str, Any], total: int | None = None, after: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "results": list(contacts),
        "total": len(contacts) if total is None else total,
    }
    if after is not None:
        body["paging"] = {"next": {"after": after}}
    return body


async def test_unlinked_phone_contact_persists_one_verified_mapping(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("701"), phone=phone))

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert result.hubspot_contact_id == _provider_id("701")
    assert mapping is not None
    assert mapping.hubspot_portal_id == PORTAL_ID
    assert mapping.hubspot_contact_id == _provider_id("701")
    assert mapping.linked_by is CRMContactLinkProvenance.PHONE_E164
    assert mapping.state is CRMContactLinkState.ACTIVE
    assert mapping.verified_at is not None
    assert [path for _, path in provider.requests] == [
        "/account-info/2026-03/details",
        "/crm/objects/2026-09/contacts/search",
    ]


async def test_email_link_persists_verified_email_and_survives_new_session(session_factory) -> None:
    email = f"verified-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory, phone=None)
    provider = MockHubSpot()
    provider.contact_by_email[email] = (200, _contact_payload(_provider_id("702"), email=email))
    provider.contact_by_id[_provider_id("702")] = (
        200,
        _contact_payload(_provider_id("702"), email=email),
    )

    first = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=email),
        provider,
    )

    mapping = await _link(session_factory, contact_id)
    assert first.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert mapping is not None and mapping.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL
    assert await _contact_email(session_factory, contact_id) == email

    second = await _resolve(
        session_factory,
        _request(contact_id, conversation_id),
        provider,
    )

    assert second.outcome is CRMContactResolutionOutcome.LINKED
    assert second.hubspot_contact_id == _provider_id("702")
    assert ("GET", f"/crm/objects/2026-09/contacts/{_provider_id('702')}") in provider.requests


async def test_existing_phone_mapping_is_freshly_verified_by_record_id(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    await _add_link(
        session_factory, contact_id, _provider_id("703"), CRMContactLinkProvenance.PHONE_E164
    )
    provider = MockHubSpot()
    provider.contact_by_id[_provider_id("703")] = (
        200,
        _contact_payload(_provider_id("703"), phone=phone),
    )

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.LINKED
    assert result.hubspot_contact_id == _provider_id("703")
    assert ("GET", f"/crm/objects/2026-09/contacts/{_provider_id('703')}") in provider.requests
    assert await _link(session_factory, contact_id) is not None


async def test_existing_email_mapping_uses_persisted_email(session_factory) -> None:
    email = f"durable-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory, phone=None)
    await _add_link(
        session_factory,
        contact_id,
        _provider_id("704"),
        CRMContactLinkProvenance.VERIFIED_EMAIL,
        email=email,
    )
    provider = MockHubSpot()
    provider.contact_by_id[_provider_id("704")] = (
        200,
        _contact_payload(_provider_id("704"), email=email),
    )

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.LINKED
    assert result.hubspot_contact_id == _provider_id("704")


async def test_existing_mapping_identity_conflict_does_not_relink(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    await _add_link(
        session_factory, contact_id, _provider_id("705"), CRMContactLinkProvenance.PHONE_E164
    )
    provider = MockHubSpot()
    provider.contact_by_id[_provider_id("705")] = (
        200,
        _contact_payload(_provider_id("705"), phone="+966551234567"),
    )
    provider.search_body = _search(_contact_payload(_provider_id("706"), phone=phone))

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.IDENTITY_CONFLICT
    assert result.hubspot_contact_id is None
    assert mapping is not None and mapping.hubspot_contact_id == _provider_id("705")
    assert mapping.state is CRMContactLinkState.ACTIVE
    assert not any(path.endswith("/contacts/search") for _, path in provider.requests)


async def test_existing_mapping_in_wrong_portal_fails_before_provider_read(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    await _add_link(
        session_factory,
        contact_id,
        _provider_id("707"),
        CRMContactLinkProvenance.PHONE_E164,
        portal_id="654321",
    )
    provider = MockHubSpot()

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.IDENTITY_CONFLICT
    assert provider.requests == []


async def test_missing_mapped_record_is_marked_stale_then_safely_relinked(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    await _add_link(
        session_factory, contact_id, _provider_id("708"), CRMContactLinkProvenance.PHONE_E164
    )
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("709"), phone=phone))

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert result.hubspot_contact_id == _provider_id("709")
    assert mapping is not None
    assert mapping.hubspot_contact_id == _provider_id("709")
    assert mapping.state is CRMContactLinkState.ACTIVE


async def test_archived_mapping_stays_stale_when_re_resolution_is_ambiguous(
    session_factory,
) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    await _add_link(
        session_factory, contact_id, _provider_id("710"), CRMContactLinkProvenance.PHONE_E164
    )
    provider = MockHubSpot()
    provider.contact_by_id[_provider_id("710")] = (
        200,
        _contact_payload(_provider_id("710"), phone=phone, archived=True),
    )
    provider.search_body = _search(
        _contact_payload(_provider_id("711"), phone=phone),
        _contact_payload(_provider_id("712"), phone=phone),
    )

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.AMBIGUOUS
    assert result.hubspot_contact_id is None
    assert mapping is not None
    assert mapping.hubspot_contact_id == _provider_id("710")
    assert mapping.state is CRMContactLinkState.STALE


async def test_stale_mapping_is_retained_when_no_replacement_is_found(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    await _add_link(
        session_factory, contact_id, _provider_id("713"), CRMContactLinkProvenance.PHONE_E164
    )
    provider = MockHubSpot()

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.STALE_MAPPING
    assert mapping is not None and mapping.hubspot_contact_id == _provider_id("713")
    assert mapping.state is CRMContactLinkState.STALE


async def test_unlinked_contact_matching_phone_and_email_to_different_records_conflicts(
    session_factory,
) -> None:
    email = f"verified-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("714"), phone=phone))
    provider.contact_by_email[email] = (200, _contact_payload(_provider_id("715"), email=email))

    result = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=email),
        provider,
    )

    assert result.outcome is CRMContactResolutionOutcome.IDENTITY_CONFLICT
    assert await _link(session_factory, contact_id) is None


async def test_unlinked_contact_with_no_provider_match_returns_not_found(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.NOT_FOUND
    assert await _link(session_factory, contact_id) is None


async def test_duplicate_provider_phone_matches_remain_ambiguous(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(
        _contact_payload(_provider_id("716"), phone=phone),
        _contact_payload(_provider_id("717"), mobilephone=phone),
    )

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.AMBIGUOUS
    assert await _link(session_factory, contact_id) is None


async def test_incomplete_provider_search_does_not_create_mapping(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(
        _contact_payload(_provider_id("718"), phone=phone), total=2, after="2"
    )

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.AMBIGUOUS
    assert await _link(session_factory, contact_id) is None


async def test_withheld_phone_does_not_trust_arbitrary_local_email(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(
        session_factory,
        phone=None,
        email=f"unverified-{uuid.uuid4().hex}@example.test",
    )
    provider = MockHubSpot()

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.UNRESOLVED_IDENTITY
    assert provider.requests == []
    assert await _link(session_factory, contact_id) is None


async def test_conflicting_local_email_is_never_overwritten(session_factory) -> None:
    local_email = f"old-{uuid.uuid4().hex}@example.test"
    trusted_email = f"new-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(
        session_factory,
        phone=None,
        email=local_email,
    )
    provider = MockHubSpot()
    provider.contact_by_email[trusted_email] = (
        200,
        _contact_payload(_provider_id("719"), email=trusted_email),
    )

    result = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=trusted_email),
        provider,
    )

    assert result.outcome is CRMContactResolutionOutcome.IDENTITY_CONFLICT
    assert await _contact_email(session_factory, contact_id) == local_email
    assert await _link(session_factory, contact_id) is None
    assert provider.requests == []


async def test_cross_contact_conversation_is_rejected_before_hubspot(session_factory) -> None:
    contact_id, _, _ = await _local_identity(session_factory)
    other_contact_id, other_conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()

    result = await _resolve(
        session_factory,
        _request(contact_id, other_conversation_id),
        provider,
    )

    assert result.outcome is CRMContactResolutionOutcome.INVALID_REQUEST
    assert provider.requests == []
    assert other_contact_id != contact_id


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, CRMContactResolutionOutcome.PERMISSION_DENIED),
        (403, CRMContactResolutionOutcome.PERMISSION_DENIED),
        (429, CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE),
        (503, CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE),
    ],
)
async def test_provider_read_errors_never_become_no_match(
    session_factory,
    status: int,
    expected: CRMContactResolutionOutcome,
) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_status = status

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is expected
    assert result.outcome is not CRMContactResolutionOutcome.NOT_FOUND
    assert await _link(session_factory, contact_id) is None


async def test_provider_timeout_is_not_a_verified_no_match(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()

    async def timeout(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contacts/search"):
            raise httpx.ReadTimeout("provider timeout", request=request)
        return await provider.handle(request)

    result = await resolve_crm_contact(
        session_factory,
        _request(contact_id, conversation_id),
        _settings(),
        transport=httpx.MockTransport(timeout),
    )

    assert result.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert await _link(session_factory, contact_id) is None


async def test_provider_error_payload_is_excluded_from_durable_json(session_factory) -> None:
    email_sentinel = f"error-pii-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_status = 503
    provider.search_body = {
        "message": f"{email_sentinel} {phone} {AUTH_SENTINEL} provider-body-sentinel"
    }
    request = _request(contact_id, conversation_id)

    result = await _resolve(session_factory, request, provider)

    async with session_factory() as session:
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        audit = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "resolve_crm_contact")
            .order_by(AuditEvent.occurred_at.desc())
        )
    durable = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "arguments": execution.sanitized_arguments if execution else None,
            "stored_result": execution.sanitized_result if execution else None,
            "audit": audit.sanitized_metadata if audit else None,
            "error_code": execution.safe_error_code if execution else None,
            "error": execution.safe_error_message if execution else None,
        }
    )

    assert result.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert execution is not None and audit is not None
    for sentinel in (email_sentinel, phone, AUTH_SENTINEL, "provider-body-sentinel"):
        assert sentinel not in durable


async def test_account_preflight_failure_prevents_contact_reads(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.portal_id = 999999

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.NOT_CONFIGURED
    assert result.error_code is not None
    assert provider.requests == [("GET", "/account-info/2026-03/details")]


async def test_preflight_permission_denial_prevents_contact_reads(session_factory) -> None:
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.account_status = 403

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    assert result.outcome is CRMContactResolutionOutcome.PERMISSION_DENIED
    assert provider.requests == [("GET", "/account-info/2026-03/details")]


async def test_same_key_replay_reverifies_mapping_with_fresh_provider_read(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    email = f"verified-{uuid.uuid4().hex}@example.test"
    provider = MockHubSpot()
    candidate = _contact_payload(_provider_id("720"), email=email, phone=phone)
    provider.search_body = _search(candidate)
    provider.contact_by_email[email] = (200, candidate)
    provider.contact_by_id[_provider_id("720")] = (200, candidate)
    request = _request(contact_id, conversation_id, email=email, key=f"replay-{uuid.uuid4().hex}")

    first = await _resolve(session_factory, request, provider)
    second = await _resolve(session_factory, request, provider)

    assert first.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert not first.replayed
    assert second.outcome is CRMContactResolutionOutcome.LINKED
    assert second.replayed
    assert (
        provider.requests.count(("GET", f"/crm/objects/2026-09/contacts/{_provider_id('720')}"))
        == 1
    )


async def test_same_idempotency_key_cannot_be_reused_for_another_contact(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    other_contact_id, other_conversation_id, _ = await _local_identity(session_factory)
    key = f"shared-{uuid.uuid4().hex}"
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("721"), phone=phone))
    first = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, key=key),
        provider,
    )
    previous_request_count = len(provider.requests)

    second = await _resolve(
        session_factory,
        _request(other_contact_id, other_conversation_id, key=key),
        provider,
    )

    assert first.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert second.outcome is CRMContactResolutionOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == previous_request_count


async def test_pii_is_excluded_from_durable_execution_and_audit_json(session_factory) -> None:
    email = f"pii-sentinel-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    candidate = _contact_payload(_provider_id("722"), email=email, phone=phone)
    provider.search_body = _search(candidate)
    provider.contact_by_email[email] = (200, candidate)
    request = _request(contact_id, conversation_id, email=email)

    result = await _resolve(session_factory, request, provider)

    async with session_factory() as session:
        execution = await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == request.idempotency_key)
        )
        audit = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "resolve_crm_contact")
            .order_by(AuditEvent.occurred_at.desc())
        )
    persisted = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "arguments": execution.sanitized_arguments if execution else None,
            "stored_result": execution.sanitized_result if execution else None,
            "audit": audit.sanitized_metadata if audit else None,
            "error": execution.safe_error_message if execution else None,
        }
    )

    assert result.hubspot_contact_id == _provider_id("722")
    assert execution is not None and audit is not None
    assert email not in persisted
    assert phone not in persisted
    assert AUTH_SENTINEL not in persisted
    assert result.next_action is CRMResolutionNextAction.NONE


async def test_database_failure_rolls_back_link_and_reports_safe_recovery(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("723"), phone=phone))

    def fail_link_insert(session, _flush_context, _instances) -> None:
        if any(isinstance(row, CRMContactLink) for row in session.new):
            raise IntegrityError("forced local link failure", {}, RuntimeError())

    event.listen(Session, "before_flush", fail_link_insert)
    try:
        result = await _resolve(
            session_factory,
            _request(contact_id, conversation_id),
            provider,
        )
    finally:
        event.remove(Session, "before_flush", fail_link_insert)

    assert result.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert result.next_action is CRMResolutionNextAction.RECONCILE_SAME_KEY
    assert await _link(session_factory, contact_id) is None
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ToolExecution)
            .where(
                ToolExecution.tool_name == "resolve_crm_contact",
                ToolExecution.conversation_id == conversation_id,
            )
        )
    assert count == 1

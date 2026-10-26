from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import receptionist.application.crm_resolution as crm_resolution
from receptionist.application.crm_resolution import (
    CRMContactResolutionOutcome,
    CRMContactResolutionRequest,
    CRMResolutionErrorCode,
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
    ToolExecutionStatus,
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
        self.search_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.contact_handler: Callable[[httpx.Request], Awaitable[httpx.Response | None]] | None = (
            None
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/account-info/2026-03/details":
            body = self.account_body or {"portalId": self.portal_id}
            return httpx.Response(self.account_status, json=body)

        if request.url.path.endswith("/contacts/search"):
            if self.search_handler is not None:
                return await self.search_handler(request)
            if self.search_exception is not None:
                raise self.search_exception
            return httpx.Response(self.search_status, json=self.search_body)

        record_id_or_email = unquote(request.url.path.rsplit("/", maxsplit=1)[-1])
        if self.contact_handler is not None:
            response = await self.contact_handler(request)
            if response is not None:
                return response
        if request.url.params.get("idProperty") == "email":
            status, body = self.contact_by_email.get(
                record_id_or_email, (404, {"message": "missing"})
            )
            return httpx.Response(status, json=body)

        status, body = self.contact_by_id.get(record_id_or_email, (404, {"message": "missing"}))
        return httpx.Response(status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def _wait_event(signal: asyncio.Event) -> None:
    await asyncio.wait_for(signal.wait(), timeout=5)


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


async def _new_conversation(session_factory, contact_id: uuid.UUID) -> uuid.UUID:
    async with session_factory.begin() as session:
        conversation = Conversation(
            contact_id=contact_id,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(conversation)
        await session.flush()
        return conversation.id


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


async def _execution_for_key(session_factory, request: CRMContactResolutionRequest):
    async with session_factory() as session:
        return await session.scalar(
            select(ToolExecution).where(
                ToolExecution.idempotency_key
                == hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            )
        )


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
        idempotency_key=key or str(uuid.uuid4()),
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
            select(ToolExecution).where(
                ToolExecution.idempotency_key
                == hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            )
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
    request = _request(contact_id, conversation_id, email=email, key=str(uuid.uuid4()))

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
    key = str(uuid.uuid4())
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


async def test_same_key_rejects_different_trusted_emails_after_no_match(session_factory) -> None:
    first_email = f"first-{uuid.uuid4().hex}@example.test"
    second_email = f"second-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory, phone=None)
    provider = MockHubSpot()
    key = str(uuid.uuid4())

    first = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=first_email, key=key),
        provider,
    )
    requests_after_first = len(provider.requests)
    provider.contact_by_email[second_email] = (
        200,
        _contact_payload(_provider_id("724"), email=second_email),
    )

    second = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=second_email, key=key),
        provider,
    )

    assert first.outcome is CRMContactResolutionOutcome.NOT_FOUND
    assert second.outcome is CRMContactResolutionOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == requests_after_first
    assert await _link(session_factory, contact_id) is None


async def test_same_key_rejects_different_trusted_emails_after_provider_error(
    session_factory,
) -> None:
    first_email = f"unavailable-{uuid.uuid4().hex}@example.test"
    second_email = f"replacement-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory, phone=None)
    provider = MockHubSpot()
    provider.contact_by_email[first_email] = (503, {"message": "provider unavailable"})
    key = str(uuid.uuid4())

    first = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=first_email, key=key),
        provider,
    )
    requests_after_first = len(provider.requests)
    provider.contact_by_email[second_email] = (
        200,
        _contact_payload(_provider_id("725"), email=second_email),
    )

    second = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=second_email, key=key),
        provider,
    )

    assert first.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert second.outcome is CRMContactResolutionOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == requests_after_first
    assert await _link(session_factory, contact_id) is None


async def test_same_key_accepts_email_case_and_outer_whitespace_normalization(
    session_factory,
) -> None:
    email = f"normalized-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory, phone=None)
    provider = MockHubSpot()
    key = str(uuid.uuid4())
    first = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=f" {email.upper()} ", key=key),
        provider,
    )
    provider_id = _provider_id("726")
    provider.contact_by_email[email] = (200, _contact_payload(provider_id, email=email))

    second = await _resolve(
        session_factory,
        _request(contact_id, conversation_id, email=email, key=key),
        provider,
    )

    assert first.outcome is CRMContactResolutionOutcome.NOT_FOUND
    assert second.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert second.hubspot_contact_id == provider_id


@pytest.mark.parametrize("first_has_email", [False, True])
async def test_same_key_rejects_changed_trusted_email_presence(
    session_factory,
    first_has_email: bool,
) -> None:
    first_email = f"presence-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(session_factory)
    provider = MockHubSpot()
    key = str(uuid.uuid4())
    first = await _resolve(
        session_factory,
        _request(
            contact_id,
            conversation_id,
            email=first_email if first_has_email else None,
            key=key,
        ),
        provider,
    )
    requests_after_first = len(provider.requests)

    second = await _resolve(
        session_factory,
        _request(
            contact_id,
            conversation_id,
            email=None if first_has_email else first_email,
            key=key,
        ),
        provider,
    )

    assert first.outcome in {
        CRMContactResolutionOutcome.NOT_FOUND,
        CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
    }
    assert second.outcome is CRMContactResolutionOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == requests_after_first


async def test_pii_is_excluded_from_durable_execution_and_audit_json(session_factory) -> None:
    email = f"pii-sentinel-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    candidate = _contact_payload(_provider_id("722"), email=email, phone=phone)
    provider.search_body = _search(candidate)
    provider.contact_by_email[email] = (200, candidate)
    request = _request(contact_id, conversation_id, email=email, key=str(uuid.uuid4()))

    result = await _resolve(session_factory, request, provider)

    async with session_factory() as session:
        execution = await session.scalar(
            select(ToolExecution).where(
                ToolExecution.idempotency_key
                == hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            )
        )
        audit = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "resolve_crm_contact")
            .order_by(AuditEvent.occurred_at.desc())
        )
    persisted = json.dumps(
        {
            "result": result.model_dump(mode="json"),
            "idempotency_key": execution.idempotency_key if execution else None,
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
    assert phone.removeprefix("+") not in persisted
    assert request.idempotency_key not in persisted
    assert AUTH_SENTINEL not in persisted
    assert result.next_action is CRMResolutionNextAction.NONE


async def test_database_failure_rolls_back_link_and_reports_safe_recovery(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(_provider_id("723"), phone=phone))
    request = _request(contact_id, conversation_id)

    def fail_link_insert(session, _flush_context, _instances) -> None:
        if any(isinstance(row, CRMContactLink) for row in session.new):
            raise IntegrityError("forced local link failure", {}, RuntimeError())

    event.listen(Session, "before_flush", fail_link_insert)
    try:
        result = await _resolve(
            session_factory,
            request,
            provider,
        )
    finally:
        event.remove(Session, "before_flush", fail_link_insert)

    assert result.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert result.next_action is CRMResolutionNextAction.RECONCILE_SAME_KEY
    assert await _link(session_factory, contact_id) is None
    recovered = await _resolve(session_factory, request, provider)
    assert recovered.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert await _link(session_factory, contact_id) is not None
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


async def test_two_conversations_for_one_contact_converge_on_one_mapping(session_factory) -> None:
    contact_id, first_conversation_id, phone = await _local_identity(session_factory)
    second_conversation_id = await _new_conversation(session_factory, contact_id)
    provider_id = _provider_id("901")
    provider = MockHubSpot()
    both_searches_started = asyncio.Event()
    search_count = 0

    async def synchronized_search(_request: httpx.Request) -> httpx.Response:
        nonlocal search_count
        search_count += 1
        if search_count == 2:
            both_searches_started.set()
        await _wait_event(both_searches_started)
        return httpx.Response(
            200,
            json=_search(_contact_payload(provider_id, phone=phone)),
        )

    provider.search_handler = synchronized_search
    results = await asyncio.wait_for(
        asyncio.gather(
            _resolve(
                session_factory,
                _request(contact_id, first_conversation_id),
                provider,
            ),
            _resolve(
                session_factory,
                _request(contact_id, second_conversation_id),
                provider,
            ),
        ),
        timeout=10,
    )

    assert [result.outcome for result in results] == [
        CRMContactResolutionOutcome.FOUND_AND_LINKED,
        CRMContactResolutionOutcome.FOUND_AND_LINKED,
    ]
    assert {result.hubspot_contact_id for result in results} == {provider_id}
    async with session_factory() as session:
        links = list(
            (
                await session.scalars(
                    select(CRMContactLink).where(CRMContactLink.contact_id == contact_id)
                )
            ).all()
        )
        executions = list(
            (
                await session.scalars(
                    select(ToolExecution).where(
                        ToolExecution.conversation_id.in_(
                            [first_conversation_id, second_conversation_id]
                        )
                    )
                )
            ).all()
        )
    assert len(links) == 1
    assert links[0].contact_id == contact_id
    assert links[0].hubspot_portal_id == PORTAL_ID
    assert links[0].hubspot_contact_id == provider_id
    assert len(executions) == 2
    assert all(execution.status is ToolExecutionStatus.SUCCEEDED for execution in executions)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Contact)) is not None


async def test_two_contacts_competing_for_one_provider_id_use_database_uniqueness(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_contact_id, first_conversation_id, first_phone = await _local_identity(session_factory)
    second_contact_id, second_conversation_id, second_phone = await _local_identity(session_factory)
    provider_id = _provider_id("902")
    provider = MockHubSpot()
    both_searches_started = asyncio.Event()
    first_inserted = asyncio.Event()
    second_insert_started = asyncio.Event()
    search_count = 0
    insert_count = 0

    async def synchronized_search(_request: httpx.Request) -> httpx.Response:
        nonlocal search_count
        search_count += 1
        if search_count == 2:
            both_searches_started.set()
        await _wait_event(both_searches_started)
        return httpx.Response(
            200,
            json=_search(
                _contact_payload(provider_id, phone=first_phone, mobilephone=second_phone)
            ),
        )

    provider.search_handler = synchronized_search
    engine = session_factory.kw["bind"].sync_engine

    def observe_provider_link_insert(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal insert_count
        if "insert into crm_contact_link" in statement.lower():
            insert_count += 1
            if insert_count == 2:
                second_insert_started.set()

    event.listen(engine, "before_cursor_execute", observe_provider_link_insert)
    original_terminalize = crm_resolution._terminalize

    async def synchronize_link_inserts(session, snapshot, result) -> None:
        if result.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED:
            if snapshot.contact_id == first_contact_id:
                await session.flush()
                first_inserted.set()
                await _wait_event(second_insert_started)
            elif snapshot.contact_id == second_contact_id:
                await _wait_event(first_inserted)
                await session.flush()
        await original_terminalize(session, snapshot, result)

    monkeypatch.setattr(crm_resolution, "_terminalize", synchronize_link_inserts)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _resolve(
                    session_factory,
                    _request(first_contact_id, first_conversation_id),
                    provider,
                ),
                _resolve(
                    session_factory,
                    _request(second_contact_id, second_conversation_id),
                    provider,
                ),
            ),
            timeout=10,
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe_provider_link_insert)

    assert insert_count == 2
    assert sorted(result.outcome.value for result in results) == sorted(
        [
            CRMContactResolutionOutcome.FOUND_AND_LINKED.value,
            CRMContactResolutionOutcome.IDENTITY_CONFLICT.value,
        ]
    )
    async with session_factory() as session:
        links = list((await session.scalars(select(CRMContactLink))).all())
    provider_owners = [link for link in links if link.hubspot_contact_id == provider_id]
    assert len(provider_owners) == 1
    assert provider_owners[0].contact_id in {first_contact_id, second_contact_id}
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Contact)) is not None


async def test_same_key_in_flight_is_not_recovered_as_an_abandoned_claim(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider_id = _provider_id("903")
    provider = MockHubSpot()
    first_search_started = asyncio.Event()
    second_search_started = asyncio.Event()
    release_first = asyncio.Event()
    first_finished = asyncio.Event()
    second_finished = asyncio.Event()
    second_claim_started = asyncio.Event()
    search_count = 0
    request = _request(contact_id, conversation_id, key=str(uuid.uuid4()))
    provider.contact_by_id[provider_id] = (
        200,
        _contact_payload(provider_id, phone=phone),
    )

    original_claim = crm_resolution._claim_tool_execution
    claim_count = 0

    async def observe_claim(*args, **kwargs):
        nonlocal claim_count
        claim_count += 1
        if claim_count == 2:
            second_claim_started.set()
        return await original_claim(*args, **kwargs)

    monkeypatch.setattr(crm_resolution, "_claim_tool_execution", observe_claim)

    async def controlled_search(_request: httpx.Request) -> httpx.Response:
        nonlocal search_count
        search_count += 1
        if search_count == 1:
            first_search_started.set()
            await _wait_event(release_first)
            return httpx.Response(
                200,
                json=_search(_contact_payload(provider_id, phone=phone)),
            )
        second_search_started.set()
        await _wait_event(first_finished)
        return httpx.Response(503, json={"message": "provider unavailable"})

    provider.search_handler = controlled_search

    async def first_call():
        result = await _resolve(session_factory, request, provider)
        first_finished.set()
        return result

    async def second_call():
        try:
            return await _resolve(session_factory, request, provider)
        finally:
            second_finished.set()

    first_task = asyncio.create_task(first_call())
    await _wait_event(first_search_started)
    second_task = asyncio.create_task(second_call())
    wait_second_search = asyncio.create_task(second_search_started.wait())
    wait_second_finish = asyncio.create_task(second_finished.wait())
    await _wait_event(second_claim_started)
    done, pending = await asyncio.wait(
        {wait_second_search, wait_second_finish},
        timeout=0.2,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert not done
    second_reached_provider = second_search_started.is_set()
    release_first.set()
    results = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    execution = await _execution_for_key(session_factory, request)
    assert execution is not None
    assert (
        second_reached_provider,
        results[0].outcome,
        results[1].outcome,
        search_count,
        execution.status,
        None if execution.sanitized_result is None else execution.sanitized_result["outcome"],
    ) == (
        False,
        CRMContactResolutionOutcome.FOUND_AND_LINKED,
        CRMContactResolutionOutcome.LINKED,
        1,
        ToolExecutionStatus.SUCCEEDED,
        CRMContactResolutionOutcome.LINKED.value,
    )
    assert results[1].replayed
    assert await _link(session_factory, contact_id) is not None
    async with session_factory() as session:
        claim_count = await session.scalar(
            select(func.count())
            .select_from(ToolExecution)
            .where(
                ToolExecution.idempotency_key
                == hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
            )
        )
    assert claim_count == 1
    replay = await _resolve(session_factory, request, provider)
    assert replay.outcome is CRMContactResolutionOutcome.LINKED
    assert replay.replayed
    assert ("GET", f"/crm/objects/2026-09/contacts/{provider_id}") in provider.requests


async def test_abandoned_started_claim_can_be_explicitly_reconciled(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider_id = _provider_id("913")
    request = _request(contact_id, conversation_id, key=str(uuid.uuid4()))
    async with session_factory.begin() as session:
        session.add(
            ToolExecution(
                conversation_id=conversation_id,
                tool_name="resolve_crm_contact",
                status=ToolExecutionStatus.STARTED,
                idempotency_key=hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest(),
                sanitized_arguments={
                    "conversation_id": str(conversation_id),
                    "contact_id": str(contact_id),
                    "trusted_email_supplied": False,
                    "trusted_email_identity_tag": None,
                },
            )
        )
    provider = MockHubSpot()
    provider.search_body = _search(_contact_payload(provider_id, phone=phone))

    result = await _resolve(session_factory, request, provider)

    execution = await _execution_for_key(session_factory, request)
    assert result.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert result.replayed
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    assert await _link(session_factory, contact_id) is not None


async def test_different_keys_on_one_conversation_respect_started_claim(session_factory) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider_id = _provider_id("904")
    provider = MockHubSpot()
    search_started = asyncio.Event()
    release_search = asyncio.Event()
    search_count = 0

    async def paused_search(_request: httpx.Request) -> httpx.Response:
        nonlocal search_count
        search_count += 1
        search_started.set()
        await _wait_event(release_search)
        return httpx.Response(200, json=_search(_contact_payload(provider_id, phone=phone)))

    provider.search_handler = paused_search
    first_request = _request(contact_id, conversation_id)
    second_request = _request(contact_id, conversation_id)
    first_task = asyncio.create_task(_resolve(session_factory, first_request, provider))
    await _wait_event(search_started)
    second = await asyncio.wait_for(
        _resolve(session_factory, second_request, provider),
        timeout=5,
    )
    release_search.set()
    first = await asyncio.wait_for(first_task, timeout=10)

    assert second.outcome is CRMContactResolutionOutcome.OPERATION_IN_PROGRESS
    assert first.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert search_count == 1
    async with session_factory() as session:
        claims = list(
            (
                await session.scalars(
                    select(ToolExecution).where(
                        ToolExecution.conversation_id == conversation_id,
                        ToolExecution.tool_name == "resolve_crm_contact",
                    )
                )
            ).all()
        )
    assert len(claims) == 1


async def test_stale_mapping_mark_and_relink_race_never_reactivates_old_record(
    session_factory,
) -> None:
    contact_id, first_conversation_id, phone = await _local_identity(session_factory)
    second_conversation_id = await _new_conversation(session_factory, contact_id)
    old_id = _provider_id("905")
    new_id = _provider_id("906")
    await _add_link(session_factory, contact_id, old_id, CRMContactLinkProvenance.PHONE_E164)
    provider = MockHubSpot()
    old_reads_started = asyncio.Event()
    old_read_count = 0

    async def controlled_contact_read(request: httpx.Request) -> httpx.Response | None:
        nonlocal old_read_count
        if request.url.path.endswith(f"/contacts/{old_id}"):
            old_read_count += 1
            if old_read_count == 2:
                old_reads_started.set()
            await _wait_event(old_reads_started)
            return httpx.Response(404, json={"message": "missing"})
        return None

    provider.contact_handler = controlled_contact_read
    provider.search_body = _search(_contact_payload(new_id, phone=phone))
    results = await asyncio.wait_for(
        asyncio.gather(
            _resolve(
                session_factory,
                _request(contact_id, first_conversation_id),
                provider,
            ),
            _resolve(
                session_factory,
                _request(contact_id, second_conversation_id),
                provider,
            ),
        ),
        timeout=10,
    )

    mapping = await _link(session_factory, contact_id)
    assert mapping is not None
    assert mapping.hubspot_contact_id == new_id
    assert mapping.state is CRMContactLinkState.ACTIVE
    assert old_id != mapping.hubspot_contact_id
    assert CRMContactResolutionOutcome.FOUND_AND_LINKED in {r.outcome for r in results}
    assert any(
        result.error_code is CRMResolutionErrorCode.LOCAL_STATE_CHANGED for result in results
    )
    assert old_read_count == 2


async def test_competing_stale_relink_candidates_cannot_overwrite_winner(session_factory) -> None:
    contact_id, first_conversation_id, phone = await _local_identity(session_factory)
    second_conversation_id = await _new_conversation(session_factory, contact_id)
    old_id = _provider_id("907")
    replacement_ids = {_provider_id("908"), _provider_id("909")}
    await _add_link(
        session_factory,
        contact_id,
        old_id,
        CRMContactLinkProvenance.PHONE_E164,
        state=CRMContactLinkState.STALE,
    )
    provider = MockHubSpot()
    both_searches_started = asyncio.Event()
    search_count = 0

    async def competing_search(_request: httpx.Request) -> httpx.Response:
        nonlocal search_count
        search_count += 1
        current = _provider_id("908") if search_count == 1 else _provider_id("909")
        if search_count == 2:
            both_searches_started.set()
        await _wait_event(both_searches_started)
        return httpx.Response(200, json=_search(_contact_payload(current, phone=phone)))

    provider.search_handler = competing_search
    results = await asyncio.wait_for(
        asyncio.gather(
            _resolve(
                session_factory,
                _request(contact_id, first_conversation_id),
                provider,
            ),
            _resolve(
                session_factory,
                _request(contact_id, second_conversation_id),
                provider,
            ),
        ),
        timeout=10,
    )

    mapping = await _link(session_factory, contact_id)
    assert mapping is not None
    assert mapping.hubspot_contact_id in replacement_ids
    assert mapping.state is CRMContactLinkState.ACTIVE
    successful = [r for r in results if r.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED]
    assert len(successful) == 1
    assert successful[0].hubspot_contact_id == mapping.hubspot_contact_id
    assert any(
        result.error_code is CRMResolutionErrorCode.LOCAL_STATE_CHANGED for result in results
    )
    assert old_id not in {r.hubspot_contact_id for r in results}


async def test_provider_failure_same_key_can_recover_without_false_no_match(
    session_factory,
) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider_id = _provider_id("910")
    provider = MockHubSpot()
    provider.search_status = 503
    request = _request(contact_id, conversation_id, key=str(uuid.uuid4()))

    failed = await _resolve(session_factory, request, provider)
    assert failed.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert await _link(session_factory, contact_id) is None

    provider.search_status = 200
    provider.search_body = _search(_contact_payload(provider_id, phone=phone))
    recovered = await _resolve(session_factory, request, provider)

    assert recovered.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert recovered.hubspot_contact_id == provider_id
    execution = await _execution_for_key(session_factory, request)
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    assert await _link(session_factory, contact_id) is not None


async def test_provider_failure_does_not_deactivate_existing_link(session_factory) -> None:
    contact_id, conversation_id, _phone = await _local_identity(session_factory)
    provider_id = _provider_id("914")
    await _add_link(
        session_factory,
        contact_id,
        provider_id,
        CRMContactLinkProvenance.PHONE_E164,
    )
    provider = MockHubSpot()
    provider.contact_by_id[provider_id] = (503, {"message": "provider unavailable"})

    result = await _resolve(session_factory, _request(contact_id, conversation_id), provider)

    mapping = await _link(session_factory, contact_id)
    assert result.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert mapping is not None
    assert mapping.hubspot_contact_id == provider_id
    assert mapping.state is CRMContactLinkState.ACTIVE


async def test_terminalization_failure_rolls_back_link_and_email_then_recovers(
    session_factory,
) -> None:
    email = f"rollback-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(
        session_factory,
        phone=None,
    )
    provider_id = _provider_id("911")
    provider = MockHubSpot()
    provider.contact_by_email[email] = (
        200,
        _contact_payload(provider_id, email=email),
    )
    request = _request(contact_id, conversation_id, email=email, key=str(uuid.uuid4()))
    fail_once = True

    def fail_terminalization(session, _flush_context, _instances) -> None:
        nonlocal fail_once
        terminalizing_execution = any(
            isinstance(row, ToolExecution)
            and row not in session.new
            and row.status is not ToolExecutionStatus.STARTED
            for row in session.dirty
        )
        if fail_once and terminalizing_execution:
            fail_once = False
            raise IntegrityError("forced execution terminalization failure", {}, RuntimeError())

    event.listen(Session, "before_flush", fail_terminalization)
    try:
        failed = await _resolve(session_factory, request, provider)
    finally:
        event.remove(Session, "before_flush", fail_terminalization)

    assert failed.outcome is CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE
    assert await _link(session_factory, contact_id) is None
    assert await _contact_email(session_factory, contact_id) is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Contact)) is not None

    recovered = await _resolve(session_factory, request, provider)
    assert recovered.outcome is CRMContactResolutionOutcome.FOUND_AND_LINKED
    assert recovered.hubspot_contact_id == provider_id
    assert (await _link(session_factory, contact_id)).hubspot_contact_id == provider_id
    assert await _contact_email(session_factory, contact_id) == email


async def test_local_phone_change_during_provider_read_invalidates_evidence(
    session_factory,
) -> None:
    contact_id, conversation_id, phone = await _local_identity(session_factory)
    provider_id = _provider_id("912")
    provider = MockHubSpot()
    search_started = asyncio.Event()
    release_search = asyncio.Event()

    async def paused_search(_request: httpx.Request) -> httpx.Response:
        search_started.set()
        await _wait_event(release_search)
        return httpx.Response(200, json=_search(_contact_payload(provider_id, phone=phone)))

    provider.search_handler = paused_search
    request = _request(contact_id, conversation_id)
    task = asyncio.create_task(_resolve(session_factory, request, provider))
    await _wait_event(search_started)
    changed_phone = f"+96655{uuid.uuid4().int % 10**7:07d}"
    async with session_factory.begin() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        contact.phone_e164 = changed_phone
    release_search.set()
    result = await asyncio.wait_for(task, timeout=10)

    assert result.error_code is CRMResolutionErrorCode.LOCAL_STATE_CHANGED
    assert result.hubspot_contact_id is None
    assert await _link(session_factory, contact_id) is None
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None and contact.phone_e164 == changed_phone


async def test_local_email_change_during_provider_read_invalidates_evidence(
    session_factory,
) -> None:
    requested_email = f"requested-{uuid.uuid4().hex}@example.test"
    changed_email = f"changed-{uuid.uuid4().hex}@example.test"
    contact_id, conversation_id, _ = await _local_identity(
        session_factory,
        phone=None,
    )
    provider_id = _provider_id("915")
    provider = MockHubSpot()
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def paused_email_lookup(_request: httpx.Request) -> httpx.Response:
        lookup_started.set()
        await _wait_event(release_lookup)
        return httpx.Response(
            200,
            json=_contact_payload(provider_id, email=requested_email),
        )

    provider.contact_handler = paused_email_lookup
    request = _request(contact_id, conversation_id, email=requested_email)
    task = asyncio.create_task(_resolve(session_factory, request, provider))
    await _wait_event(lookup_started)
    async with session_factory.begin() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        contact.email = changed_email
    release_lookup.set()
    result = await asyncio.wait_for(task, timeout=10)

    assert result.error_code is CRMResolutionErrorCode.LOCAL_STATE_CHANGED
    assert result.hubspot_contact_id is None
    assert await _link(session_factory, contact_id) is None
    assert await _contact_email(session_factory, contact_id) == changed_email


async def test_concurrent_wrong_portal_preflight_creates_no_link_or_pii_audit(
    session_factory,
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    email = f"portal-pii-{uuid.uuid4().hex}@example.test"
    contact_id, first_conversation_id, _ = await _local_identity(
        session_factory,
        phone=phone,
    )
    second_conversation_id = await _new_conversation(session_factory, contact_id)
    provider = MockHubSpot()
    provider.portal_id += 1
    requests = [
        _request(contact_id, first_conversation_id, email=email),
        _request(contact_id, second_conversation_id, email=email),
    ]
    results = await asyncio.wait_for(
        asyncio.gather(*(_resolve(session_factory, request, provider) for request in requests)),
        timeout=10,
    )

    assert all(result.outcome is CRMContactResolutionOutcome.NOT_CONFIGURED for result in results)
    assert await _link(session_factory, contact_id) is None
    assert all(path == "/account-info/2026-03/details" for _, path in provider.requests)
    executions = [await _execution_for_key(session_factory, request) for request in requests]
    async with session_factory() as session:
        audits = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == "resolve_crm_contact")
                )
            ).all()
        )
    durable = json.dumps(
        {
            "results": [result.model_dump(mode="json") for result in results],
            "executions": [
                {
                    "arguments": execution.sanitized_arguments if execution else None,
                    "result": execution.sanitized_result if execution else None,
                    "error": execution.safe_error_message if execution else None,
                }
                for execution in executions
            ],
            "audits": [audit.sanitized_metadata for audit in audits],
        }
    )
    assert phone not in durable
    assert email not in durable
    assert AUTH_SENTINEL not in durable

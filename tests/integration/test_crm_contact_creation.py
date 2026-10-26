from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from receptionist.application import crm_contact_creation as crm_creation
from receptionist.application.crm_contact_creation import (
    _claim_create_operation,
    _trusted_evidence_tags,
    create_crm_contact,
)
from receptionist.application.crm_contact_writes import (
    CRMContactCreateOutcome,
    CRMContactCreateRequest,
    CRMContactCreateResult,
    CRMContactWriteErrorCode,
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
    return f"{_PROVIDER_RUN_PREFIX}{suffix}"


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


def _payload(
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
        self.search_status = 200
        self.search_body: object = {"results": [], "total": 0}
        self.search_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.email_records: dict[str, tuple[int, object]] = {}
        self.email_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.create_status = 201
        self.create_body: object | None = None
        self.create_exception: BaseException | None = None
        self.record_before_create_response = True
        self.account_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.requests: list[tuple[str, str]] = []
        self.create_payloads: list[dict[str, Any]] = []
        self.create_count = 0
        self.created_ids: list[str] = []
        self.create_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None
        self.before_create_response: Callable[[], Awaitable[None]] | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/account-info/2026-03/details":
            if self.account_handler is not None:
                return await self.account_handler(request)
            return httpx.Response(self.account_status, json={"portalId": self.portal_id})

        if request.url.path.endswith("/contacts/search"):
            if self.search_handler is not None:
                return await self.search_handler(request)
            return httpx.Response(self.search_status, json=self.search_body)

        if request.method == "GET" and request.url.params.get("idProperty") == "email":
            if self.email_handler is not None:
                return await self.email_handler(request)
            email = unquote(request.url.path.rsplit("/", maxsplit=1)[-1]).casefold()
            status, body = self.email_records.get(email, (404, {"message": "missing"}))
            return httpx.Response(status, json=body)

        if request.method == "POST" and request.url.path == "/crm/objects/2026-09/contacts":
            self.create_count += 1
            body = json.loads(request.content)
            self.create_payloads.append(body)
            if self.create_handler is not None:
                return await self.create_handler(request)
            properties = body["properties"]
            record_id = _provider_id(str(uuid.uuid4().int % 10**10))
            self.created_ids.append(record_id)
            response_body = self.create_body or _payload(record_id, **properties)
            if self.record_before_create_response:
                self.email_records[str(properties["email"]).casefold()] = (200, response_body)
            if self.before_create_response is not None:
                await self.before_create_response()
            if self.create_exception is not None:
                raise self.create_exception
            return httpx.Response(self.create_status, json=response_body)

        return httpx.Response(404, json={"message": "missing"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def _owner(
    session_factory,
    *,
    phone: str | None = None,
    email: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
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
        return contact.id, conversation.id


def _request(
    contact_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    email: str | None = None,
    phone: str | None = None,
    key: str | None = None,
) -> CRMContactCreateRequest:
    return CRMContactCreateRequest(
        contact_id=contact_id,
        conversation_id=conversation_id,
        idempotency_key=key or str(uuid.uuid4()),
        trusted_verified_email=email or f"create-{uuid.uuid4().hex}@example.test",
        trusted_verified_phone=phone,
    )


async def _create(session_factory, request, provider: MockHubSpot, *, settings=None):
    return await create_crm_contact(
        session_factory,
        request,
        settings or _settings(),
        transport=provider.transport(),
    )


async def _link(session_factory, contact_id: uuid.UUID) -> CRMContactLink | None:
    async with session_factory() as session:
        return await session.get(CRMContactLink, contact_id)


async def _execution(session_factory, request: CRMContactCreateRequest) -> ToolExecution | None:
    key = hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
    async with session_factory() as session:
        return await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == key)
        )


async def test_successful_email_only_create_claims_before_post_and_links_atomically(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    async def inspect_claim_before_post() -> None:
        execution = await _execution(session_factory, request)
        assert execution is not None
        assert execution.status is ToolExecutionStatus.STARTED
        assert execution.sanitized_result is None

    provider.before_create_response = inspect_claim_before_post

    result = await _create(session_factory, request, provider)

    link = await _link(session_factory, contact_id)
    execution = await _execution(session_factory, request)
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "create_crm_contact")
        )
    assert result.outcome is CRMContactCreateOutcome.CREATED
    assert result.hubspot_contact_id == provider.created_ids[0]
    assert provider.create_count == 1
    assert provider.create_payloads == [{"properties": {"email": request.trusted_verified_email}}]
    assert link is not None and link.hubspot_contact_id == result.hubspot_contact_id
    assert link.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL
    assert contact is not None and contact.email == request.trusted_verified_email
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    assert execution.sanitized_arguments["trusted_email_identity_tag"]
    assert request.trusted_verified_email not in json.dumps(execution.sanitized_arguments)
    assert request.idempotency_key not in execution.idempotency_key
    assert audit is not None
    assert request.trusted_verified_email not in json.dumps(audit.sanitized_metadata)
    assert request.idempotency_key not in json.dumps(audit.sanitized_metadata)


async def test_create_includes_only_email_and_explicit_verified_phone(session_factory) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    contact_id, conversation_id = await _owner(session_factory, phone=phone)
    request = _request(contact_id, conversation_id, phone=phone)
    provider = MockHubSpot()

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.CREATED
    assert provider.create_payloads == [
        {"properties": {"email": request.trusted_verified_email, "phone": phone}}
    ]
    execution = await _execution(session_factory, request)
    assert execution is not None
    assert phone not in json.dumps(execution.sanitized_arguments)
    assert request.trusted_verified_email not in json.dumps(execution.sanitized_arguments)
    assert request.idempotency_key not in json.dumps(execution.sanitized_arguments)
    async with session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == "create_crm_contact")
                )
            ).all()
        )
    assert all(phone not in json.dumps(item.sanitized_metadata) for item in events)


async def test_create_without_returned_email_stays_uncertain_until_durable_email_match(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    request = _request(contact_id, conversation_id, phone=phone)
    provider = MockHubSpot()
    provider.create_body = {"properties": {"phone": phone}}

    first = await _create(session_factory, request, provider)
    link_before_reconciliation = await _link(session_factory, contact_id)
    provider.email_records[request.trusted_verified_email] = (
        200,
        _payload(
            provider.created_ids[0],
            email=request.trusted_verified_email,
            phone=phone,
        ),
    )
    recovered = await _create(session_factory, request, provider)
    link_after_reconciliation = await _link(session_factory, contact_id)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert link_before_reconciliation is None
    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert provider.create_count == 1
    assert link_after_reconciliation is not None
    assert link_after_reconciliation.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL


async def test_matching_existing_provider_contact_blocks_post(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.email_records[request.trusted_verified_email] = (
        200,
        _payload(_provider_id("2"), email=request.trusted_verified_email),
    )

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.EXISTING_CONTACT
    assert result.hubspot_contact_id == _provider_id("2")
    assert provider.create_count == 0
    assert await _link(session_factory, contact_id) is None


async def test_incomplete_phone_search_blocks_contact_post(session_factory) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    contact_id, conversation_id = await _owner(session_factory, phone=phone)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.search_body = {"results": [], "total": 50}

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.IDENTITY_CONFLICT
    assert provider.create_count == 0
    assert await _link(session_factory, contact_id) is None


async def test_wrong_account_preflight_blocks_post(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.portal_id += 1

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.NOT_CONFIGURED
    assert result.error_code is CRMContactWriteErrorCode.ACCOUNT_MISMATCH
    assert provider.create_count == 0


async def test_portal_change_after_claim_prevents_any_provider_request(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    settings = _settings()
    provider = MockHubSpot()
    original_claim = crm_creation._claim_create_operation

    async def claim_then_switch_portal(*args, **kwargs):
        result = await original_claim(*args, **kwargs)
        settings.hubspot_portal_id = "654321"
        return result

    monkeypatch.setattr(crm_creation, "_claim_create_operation", claim_then_switch_portal)
    result = await create_crm_contact(
        session_factory,
        request,
        settings,
        transport=provider.transport(),
    )

    assert result.outcome is CRMContactCreateOutcome.NOT_CONFIGURED
    assert result.error_code is CRMContactWriteErrorCode.ACCOUNT_MISMATCH
    assert provider.requests == []


async def test_unverified_production_capabilities_do_not_enable_mocked_write(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    result = await _create(session_factory, request, provider, settings=_settings(app_env="local"))

    assert result.outcome is CRMContactCreateOutcome.NOT_CONFIGURED
    assert result.error_code is CRMContactWriteErrorCode.ACTIVATION_REQUIRED
    assert provider.create_count == 0


async def test_ambiguous_phone_search_blocks_post(session_factory) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    contact_id, conversation_id = await _owner(session_factory, phone=phone)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.search_body = {
        "results": [
            _payload(_provider_id("31"), phone=phone),
            _payload(_provider_id("32"), phone=phone),
        ],
        "total": 2,
    }

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.IDENTITY_CONFLICT
    assert result.error_code is CRMContactWriteErrorCode.AMBIGUOUS_IDENTITY
    assert provider.create_count == 0


async def test_phone_and_email_resolving_to_different_contacts_blocks_post(
    session_factory,
) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    contact_id, conversation_id = await _owner(session_factory, phone=phone)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.search_body = {
        "results": [_payload(_provider_id("33"), phone=phone)],
        "total": 1,
    }
    provider.email_records[request.trusted_verified_email] = (
        200,
        _payload(_provider_id("34"), email=request.trusted_verified_email),
    )

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.IDENTITY_CONFLICT
    assert result.error_code is CRMContactWriteErrorCode.IDENTITY_CONFLICT
    assert provider.create_count == 0


async def test_cross_contact_conversation_ownership_blocks_all_provider_requests(
    session_factory,
) -> None:
    _, conversation_id = await _owner(session_factory)
    other_contact, _ = await _owner(session_factory)
    request = _request(other_contact, conversation_id)
    provider = MockHubSpot()

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.OWNERSHIP_REJECTED
    assert provider.requests == []


@pytest.mark.parametrize("status", [401, 403])
async def test_account_permission_failure_blocks_contact_mutation(
    session_factory, status: int
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.account_status = status

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.FAILED
    assert result.error_code is CRMContactWriteErrorCode.PERMISSION_DENIED
    assert provider.create_count == 0


@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_definite_create_rejection_is_terminal_and_not_reposted(
    session_factory,
    status: int,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = status
    provider.record_before_create_response = False

    first = await _create(session_factory, request, provider)
    request_count = len(provider.requests)
    replay = await _create(session_factory, request, provider)

    assert first.outcome is CRMContactCreateOutcome.FAILED
    assert replay.outcome is CRMContactCreateOutcome.FAILED
    assert replay.replayed is True
    assert provider.create_count == 1
    assert len(provider.requests) == request_count
    assert await _link(session_factory, contact_id) is None


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (400, CRMContactWriteErrorCode.PROVIDER_VALIDATION),
        (401, CRMContactWriteErrorCode.PERMISSION_DENIED),
        (403, CRMContactWriteErrorCode.PERMISSION_DENIED),
        (429, CRMContactWriteErrorCode.RATE_LIMITED),
    ],
)
async def test_create_status_errors_are_finite_and_safe(
    session_factory,
    status: int,
    error_code: CRMContactWriteErrorCode,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = status
    provider.record_before_create_response = False

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.FAILED
    assert result.error_code is error_code
    serialized = result.model_dump_json()
    assert request.trusted_verified_email not in serialized
    assert AUTH_SENTINEL not in serialized
    assert await _link(session_factory, contact_id) is None


async def test_uncertain_5xx_same_key_reconciles_without_second_post(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = 500

    first = await _create(session_factory, request, provider)
    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert await _link(session_factory, contact_id) is None

    recovered = await _create(session_factory, request, provider)

    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert recovered.hubspot_contact_id == provider.created_ids[0]
    assert provider.create_count == 1
    mapping = await _link(session_factory, contact_id)
    assert mapping is not None and mapping.hubspot_contact_id == provider.created_ids[0]
    execution = await _execution(session_factory, request)
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED


async def test_accepted_malformed_201_response_stays_uncertain_and_recovers(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_body = {"properties": {"email": request.trusted_verified_email}}

    first = await _create(session_factory, request, provider)
    provider.email_records[request.trusted_verified_email] = (
        200,
        _payload(_provider_id("36"), email=request.trusted_verified_email),
    )
    recovered = await _create(session_factory, request, provider)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_delayed_email_visibility_keeps_uncertain_claim_without_repost(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = 500

    first = await _create(session_factory, request, provider)
    provider.email_records.pop(request.trusted_verified_email)
    delayed = await _create(session_factory, request, provider)
    provider.email_records[request.trusted_verified_email] = (
        200,
        _payload(provider.created_ids[0], email=request.trusted_verified_email),
    )
    recovered = await _create(session_factory, request, provider)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert delayed.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_http_409_reconciles_exact_existing_record_without_second_post(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    async def conflict(request_message: httpx.Request) -> httpx.Response:
        properties = json.loads(request_message.content)["properties"]
        record_id = _provider_id("37")
        provider.email_records[properties["email"]] = (
            200,
            _payload(record_id, **properties),
        )
        return httpx.Response(409, json={"message": "duplicate"})

    provider.create_handler = conflict

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.EXISTING_CONTACT
    assert result.hubspot_contact_id == _provider_id("37")
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is None


async def test_claim_persistence_failure_prevents_every_hubspot_request(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    engine = session_factory.kw["bind"].sync_engine

    def fail_claim(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if "insert into tool_execution" in statement.lower():
            raise OperationalError("injected claim failure", {}, RuntimeError())

    event.listen(engine, "before_cursor_execute", fail_claim)
    try:
        result = await _create(session_factory, request, provider)
    finally:
        event.remove(engine, "before_cursor_execute", fail_claim)

    assert result.outcome is CRMContactCreateOutcome.FAILED
    assert result.error_code is CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE
    assert provider.requests == []


async def test_interrupted_committed_claim_before_post_only_reconciles_read_only(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    settings = _settings()
    email_tag, phone_tag = _trusted_evidence_tags(
        request.idempotency_key,
        email=request.trusted_verified_email,
        phone=request.trusted_verified_phone,
    )
    claim = await _claim_create_operation(
        session_factory,
        request,
        email_tag=email_tag,
        phone_tag=phone_tag,
        settings=settings,
    )
    assert not isinstance(claim, CRMContactCreateResult)
    provider = MockHubSpot()

    resumed = await _create(session_factory, request, provider)
    execution = await _execution(session_factory, request)

    assert resumed.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.create_count == 0
    assert all(path != "/crm/objects/2026-09/contacts" for _, path in provider.requests)


async def test_provider_success_local_link_failure_recovers_without_posting_again(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    def fail_link(session, _flush_context, _instances) -> None:
        if any(isinstance(row, CRMContactLink) for row in session.new):
            raise IntegrityError("injected link persistence failure", {}, RuntimeError())

    event.listen(Session, "before_flush", fail_link)
    try:
        first = await _create(session_factory, request, provider)
    finally:
        event.remove(Session, "before_flush", fail_link)
    execution = await _execution(session_factory, request)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert await _link(session_factory, contact_id) is None

    recovered = await _create(session_factory, request, provider)

    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_same_key_concurrent_calls_issue_one_post_and_one_claim(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    both_preflights = asyncio.Event()
    preflight_count = 0

    async def synchronized_preflight(_message: httpx.Request) -> httpx.Response:
        nonlocal preflight_count
        preflight_count += 1
        if preflight_count == 2:
            both_preflights.set()
        await asyncio.wait_for(both_preflights.wait(), timeout=5)
        return httpx.Response(200, json={"portalId": int(PORTAL_ID)})

    provider.account_handler = synchronized_preflight
    results = await asyncio.wait_for(
        asyncio.gather(
            _create(session_factory, request, provider),
            _create(session_factory, request, provider),
        ),
        timeout=10,
    )

    assert provider.create_count == 1, (
        [
            (result.outcome, result.error_code, result.next_action, result.replayed)
            for result in results
        ],
        provider.requests,
    )
    assert (
        sum(
            result.outcome in {CRMContactCreateOutcome.CREATED, CRMContactCreateOutcome.REPLAYED}
            for result in results
        )
        >= 1
    )
    execution = await _execution(session_factory, request)
    assert execution is not None
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count(ToolExecution.id)).where(
                ToolExecution.tool_name == "create_crm_contact",
                ToolExecution.conversation_id == conversation_id,
            )
        )
    assert count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_different_keys_for_same_contact_cannot_overlap_mutations(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    first_request = _request(contact_id, conversation_id)
    second_request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    first_preflight = asyncio.Event()
    release_first = asyncio.Event()

    async def blocked_preflight(_message: httpx.Request) -> httpx.Response:
        first_preflight.set()
        await asyncio.wait_for(release_first.wait(), timeout=5)
        return httpx.Response(200, json={"portalId": int(PORTAL_ID)})

    provider.account_handler = blocked_preflight
    first_task = asyncio.create_task(_create(session_factory, first_request, provider))
    await asyncio.wait_for(first_preflight.wait(), timeout=5)
    second = await asyncio.wait_for(_create(session_factory, second_request, provider), timeout=5)
    release_first.set()
    first = await asyncio.wait_for(first_task, timeout=5)

    assert second.outcome is CRMContactCreateOutcome.OPERATION_IN_PROGRESS
    assert first.outcome is CRMContactCreateOutcome.CREATED
    assert provider.create_count == 1
    async with session_factory() as session:
        executions = list(
            (
                await session.scalars(
                    select(ToolExecution).where(
                        ToolExecution.tool_name == "create_crm_contact",
                        ToolExecution.conversation_id == conversation_id,
                    )
                )
            ).all()
        )
    assert len(executions) == 1


async def test_two_local_contacts_cannot_link_the_same_provider_record(
    session_factory,
) -> None:
    first_contact, first_conversation = await _owner(session_factory)
    second_contact, second_conversation = await _owner(session_factory)
    email = f"shared-{uuid.uuid4().hex}@example.test"
    first_request = _request(first_contact, first_conversation, email=email)
    second_request = _request(second_contact, second_conversation, email=email)
    provider = MockHubSpot()
    both_email_reads = asyncio.Event()
    both_creates = asyncio.Event()
    email_count = 0
    create_count = 0
    shared_provider_id = _provider_id("38")

    async def synchronized_email_read(_message: httpx.Request) -> httpx.Response:
        nonlocal email_count
        email_count += 1
        if email_count == 2:
            both_email_reads.set()
        await asyncio.wait_for(both_email_reads.wait(), timeout=5)
        return httpx.Response(404, json={"message": "missing"})

    async def synchronized_create(message: httpx.Request) -> httpx.Response:
        nonlocal create_count
        create_count += 1
        if create_count == 2:
            both_creates.set()
        await asyncio.wait_for(both_creates.wait(), timeout=5)
        properties = json.loads(message.content)["properties"]
        return httpx.Response(201, json=_payload(shared_provider_id, **properties))

    provider.email_handler = synchronized_email_read
    provider.create_handler = synchronized_create
    results = await asyncio.wait_for(
        asyncio.gather(
            _create(session_factory, first_request, provider),
            _create(session_factory, second_request, provider),
        ),
        timeout=10,
    )

    async with session_factory() as session:
        links = list(
            (
                await session.scalars(
                    select(CRMContactLink).where(
                        CRMContactLink.hubspot_portal_id == PORTAL_ID,
                        CRMContactLink.hubspot_contact_id == shared_provider_id,
                    )
                )
            ).all()
        )
    assert len(links) == 1
    assert links[0].contact_id in {first_contact, second_contact}
    assert provider.create_count == 2
    assert sorted(result.outcome.value for result in results) == sorted(
        [CRMContactCreateOutcome.CREATED.value, CRMContactCreateOutcome.UNCERTAIN.value]
    )


async def test_same_key_cannot_be_reused_for_another_contact_or_operation(session_factory) -> None:
    first_contact, first_conversation = await _owner(session_factory)
    second_contact, second_conversation = await _owner(session_factory)
    key = str(uuid.uuid4())
    first_request = _request(first_contact, first_conversation, key=key)
    provider = MockHubSpot()
    provider.create_status = 400
    provider.record_before_create_response = False
    await _create(session_factory, first_request, provider)
    second_request = _request(
        second_contact,
        second_conversation,
        key=key,
        email=first_request.trusted_verified_email,
    )
    request_count = len(provider.requests)

    owner_conflict = await _create(session_factory, second_request, provider)

    assert owner_conflict.outcome is CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == request_count

    operation_key = str(uuid.uuid4())
    other_operation_request = _request(first_contact, first_conversation, key=operation_key)
    durable_key = hashlib.sha256(operation_key.encode("ascii")).hexdigest()
    async with session_factory.begin() as session:
        session.add(
            ToolExecution(
                conversation_id=first_conversation,
                tool_name="update_crm_contact",
                status=ToolExecutionStatus.FAILED,
                idempotency_key=durable_key,
                sanitized_arguments={"contact_id": str(first_contact)},
            )
        )
    request_count = len(provider.requests)

    operation_conflict = await _create(session_factory, other_operation_request, provider)

    assert operation_conflict.outcome is CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == request_count


async def test_test_environment_without_mock_transport_cannot_reach_provider(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)

    result = await create_crm_contact(
        session_factory,
        request,
        _settings(),
        transport=None,
    )

    assert result.outcome is CRMContactCreateOutcome.NOT_CONFIGURED
    assert result.error_code is CRMContactWriteErrorCode.NOT_CONFIGURED


async def test_uncertain_post_with_no_visible_record_remains_started_and_is_not_reposted(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = 500
    provider.record_before_create_response = False

    first = await _create(session_factory, request, provider)
    second = await _create(session_factory, request, provider)
    execution = await _execution(session_factory, request)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert second.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is None


async def test_post_read_timeout_after_provider_side_effect_recovers_read_only(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_exception = httpx.ReadTimeout("private provider timeout")

    first = await _create(session_factory, request, provider)
    recovered = await _create(session_factory, request, provider)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert recovered.outcome is CRMContactCreateOutcome.REPLAYED
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_same_key_retries_a_definite_prewrite_read_failure(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    email_read_count = 0

    async def unavailable_once(_message: httpx.Request) -> httpx.Response:
        nonlocal email_read_count
        email_read_count += 1
        if email_read_count == 1:
            return httpx.Response(503, json={"message": "private provider body"})
        return httpx.Response(404, json={"message": "missing"})

    provider.email_handler = unavailable_once
    first = await _create(session_factory, request, provider)
    second = await _create(session_factory, request, provider)

    assert first.outcome is CRMContactCreateOutcome.FAILED
    assert first.error_code is CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
    assert second.outcome is CRMContactCreateOutcome.CREATED
    assert email_read_count == 2
    assert provider.create_count == 1
    assert await _link(session_factory, contact_id) is not None


async def test_conflict_read_failure_remains_uncertain_and_never_reposts(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()
    provider.create_status = 409
    email_read_count = 0

    async def unavailable_after_conflict(_message: httpx.Request) -> httpx.Response:
        nonlocal email_read_count
        email_read_count += 1
        if email_read_count == 1:
            return httpx.Response(404, json={"message": "missing"})
        return httpx.Response(503, json={"message": "private provider body"})

    provider.email_handler = unavailable_after_conflict
    first = await _create(session_factory, request, provider)
    second = await _create(session_factory, request, provider)
    execution = await _execution(session_factory, request)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert second.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert provider.create_count == 1
    assert email_read_count == 3


async def test_successful_same_key_replay_returns_stored_result_without_provider_read(
    session_factory,
) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    created = await _create(session_factory, request, provider)
    request_count = len(provider.requests)
    replay = await _create(session_factory, request, provider)

    assert created.outcome is CRMContactCreateOutcome.CREATED
    assert replay.outcome is CRMContactCreateOutcome.REPLAYED
    assert replay.replayed is True
    assert replay.hubspot_contact_id == created.hubspot_contact_id
    assert len(provider.requests) == request_count
    assert provider.create_count == 1


async def test_same_key_cannot_change_email_after_uncertain_no_match(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    key = str(uuid.uuid4())
    first_request = _request(contact_id, conversation_id, key=key)
    provider = MockHubSpot()
    provider.create_status = 500
    provider.record_before_create_response = False
    first = await _create(session_factory, first_request, provider)
    changed_request = _request(
        contact_id,
        conversation_id,
        key=key,
        email=f"changed-{uuid.uuid4().hex}@example.test",
    )
    request_count = len(provider.requests)

    conflict = await _create(session_factory, changed_request, provider)

    assert first.outcome is CRMContactCreateOutcome.UNCERTAIN
    assert conflict.outcome is CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == request_count
    assert provider.create_count == 1


async def test_same_key_cannot_change_phone_evidence(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory, phone=None)
    key = str(uuid.uuid4())
    email = f"phone-bind-{uuid.uuid4().hex}@example.test"
    first_request = _request(
        contact_id, conversation_id, key=key, email=email, phone="+966501234567"
    )
    provider = MockHubSpot()
    provider.create_status = 500
    provider.record_before_create_response = False
    await _create(session_factory, first_request, provider)
    changed_request = _request(
        contact_id, conversation_id, key=key, email=email, phone="+966551234567"
    )
    request_count = len(provider.requests)

    conflict = await _create(session_factory, changed_request, provider)

    assert conflict.outcome is CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == request_count
    assert provider.create_count == 1


async def test_same_key_is_bound_to_original_portal(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    key = str(uuid.uuid4())
    request = _request(contact_id, conversation_id, key=key)
    provider = MockHubSpot()
    provider.create_status = 500
    await _create(session_factory, request, provider)
    changed_account = _settings(portal_id="654321")
    request_count = len(provider.requests)

    replay = await create_crm_contact(
        session_factory,
        request,
        changed_account,
        transport=httpx.MockTransport(provider.handle),
    )

    assert replay.outcome is CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT
    assert len(provider.requests) == request_count
    assert provider.create_count == 1


async def test_active_or_stale_mapping_blocks_create(session_factory) -> None:
    contact_id, conversation_id = await _owner(session_factory)
    async with session_factory.begin() as session:
        session.add(
            CRMContactLink(
                contact_id=contact_id,
                hubspot_portal_id=PORTAL_ID,
                hubspot_contact_id=_provider_id("35"),
                linked_by=CRMContactLinkProvenance.VERIFIED_EMAIL,
                state=CRMContactLinkState.STALE,
            )
        )
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.IDENTITY_CONFLICT
    assert provider.requests == []


async def test_local_identity_change_during_provider_read_prevents_post(session_factory) -> None:
    phone = f"+96650{uuid.uuid4().int % 10**7:07d}"
    contact_id, conversation_id = await _owner(session_factory, phone=phone)
    request = _request(contact_id, conversation_id)
    provider = MockHubSpot()

    async def change_contact(_request: httpx.Request) -> httpx.Response:
        async with session_factory.begin() as session:
            contact = await session.get(Contact, contact_id)
            assert contact is not None
            contact.email = f"changed-{uuid.uuid4().hex}@example.test"
        return httpx.Response(200, json={"results": [], "total": 0})

    provider.search_handler = change_contact

    result = await _create(session_factory, request, provider)

    assert result.outcome is CRMContactCreateOutcome.IDENTITY_CONFLICT
    assert result.error_code is CRMContactWriteErrorCode.LOCAL_STATE_CHANGED
    assert provider.create_count == 0

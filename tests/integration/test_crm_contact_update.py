from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from receptionist.application.crm_contact_writes import (
    CRMContactUpdateOutcome,
    CRMContactUpdateRequest,
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
PROVIDER_CONTACT_ID = "987654321"
MOCK_CREDENTIAL = "mock-credential-value"
TEST_DATABASE_URL = "postgresql+psycopg://receptionist@localhost/receptionist_test"


def _settings(*, app_env: str = "test", portal_id: str = PORTAL_ID) -> Settings:
    return Settings(
        _env_file=None,
        app_env=app_env,
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        hubspot_service_key=MOCK_CREDENTIAL,
        hubspot_portal_id=portal_id,
        hubspot_request_timeout_seconds=1.0,
    )


def _payload(
    *,
    email: str | None = None,
    phone: str | None = None,
    mobilephone: str | None = None,
    archived: bool = False,
) -> dict[str, object]:
    return {
        "id": PROVIDER_CONTACT_ID,
        "properties": {"email": email, "phone": phone, "mobilephone": mobilephone},
        "archived": archived,
    }


class MockHubSpotUpdate:
    def __init__(self, *, email: str | None = None, phone: str | None = None) -> None:
        self.contact_id = PROVIDER_CONTACT_ID
        self.portal_id = int(PORTAL_ID)
        self.contact_body = _payload(email=email, phone=phone)
        self.get_status = 200
        self.search_error_after_patch: int | None = None
        self.patch_status = 200
        self.patch_body: object | None = None
        self.patch_content: bytes | None = None
        self.patch_exception: BaseException | None = None
        self.apply_patch = True
        self.search_body: object | None = None
        self.email_records: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str]] = []
        self.patch_payloads: list[dict[str, object]] = []
        self.patch_count = 0
        self.direct_read_count = 0
        self.before_patch: Callable[[], Awaitable[None]] | None = None
        self.contact_read_handler: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = (
            None
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/account-info/2026-03/details":
            return httpx.Response(200, json={"portalId": self.portal_id})
        if request.url.path.endswith("/contacts/search"):
            if self.patch_count and self.search_error_after_patch is not None:
                return httpx.Response(
                    self.search_error_after_patch,
                    json={"message": "PRIVATE_SEARCH_ERROR"},
                )
            if self.search_body is not None:
                return httpx.Response(200, json=self.search_body)
            props = self.contact_body["properties"]
            return httpx.Response(200, json={"results": [self.contact_body], "total": 1})
        if request.method == "GET" and request.url.params.get("idProperty") == "email":
            email = unquote(request.url.path.rsplit("/", maxsplit=1)[-1]).casefold()
            body = self.email_records.get(email)
            props = self.contact_body["properties"]
            provider_email = props.get("email") if isinstance(props, dict) else None
            if (
                body is None
                and isinstance(provider_email, str)
                and provider_email.casefold() == email
            ):
                body = self.contact_body
            if body is None:
                return httpx.Response(404, json={"message": "missing"})
            return httpx.Response(200, json=body)
        if request.method == "GET" and request.url.path.endswith(f"/contacts/{self.contact_id}"):
            self.direct_read_count += 1
            if self.contact_read_handler is not None:
                return await self.contact_read_handler(request)
            if self.get_status != 200:
                return httpx.Response(self.get_status, json={"message": "read failed"})
            return httpx.Response(200, json=self.contact_body)
        if request.method == "PATCH" and request.url.path.endswith(f"/contacts/{self.contact_id}"):
            self.patch_count += 1
            payload = json.loads(request.content)
            self.patch_payloads.append(payload)
            if self.before_patch is not None:
                await self.before_patch()
            if self.apply_patch and (
                self.patch_exception is not None or 200 <= self.patch_status < 300
            ):
                properties = self.contact_body["properties"]
                assert isinstance(properties, dict)
                properties.update(payload["properties"])
            if self.patch_exception is not None:
                raise self.patch_exception
            if self.patch_content is not None:
                return httpx.Response(
                    self.patch_status,
                    content=self.patch_content,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                self.patch_status,
                json=self.patch_body if self.patch_body is not None else self.contact_body,
            )
        return httpx.Response(404, json={"message": "missing"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def _owner(
    session_factory,
    provider: MockHubSpotUpdate,
    *,
    phone: str | None,
    email: str | None,
    linked_by: CRMContactLinkProvenance = CRMContactLinkProvenance.PHONE_E164,
    state: CRMContactLinkState = CRMContactLinkState.ACTIVE,
    create_link: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    provider.contact_id = str(900_000_000 + uuid.uuid4().int % 99_000_000)
    provider.contact_body["id"] = provider.contact_id
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
        if create_link:
            session.add(
                CRMContactLink(
                    contact_id=contact.id,
                    hubspot_portal_id=PORTAL_ID,
                    hubspot_contact_id=provider.contact_id,
                    linked_by=linked_by,
                    state=state,
                )
            )
        await session.flush()
        return contact.id, conversation.id


def _request(
    contact_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    email: str | None = None,
    phone: str | None = None,
    key: str | None = None,
) -> CRMContactUpdateRequest:
    return CRMContactUpdateRequest(
        contact_id=contact_id,
        conversation_id=conversation_id,
        idempotency_key=key or str(uuid.uuid4()),
        trusted_verified_email=email,
        trusted_verified_phone=phone,
    )


def _phone() -> str:
    suffix = uuid.uuid4().int % 10_000_000
    return f"+96650{suffix:07d}"


async def _update(session_factory, request, provider, *, settings=None):
    try:
        from receptionist.application.crm_contact_update import update_crm_contact
    except ImportError:
        pytest.fail("Task 10 update operation has not been implemented")
    return await update_crm_contact(
        session_factory,
        request,
        settings or _settings(),
        transport=provider.transport(),
    )


async def _execution(session_factory, request: CRMContactUpdateRequest) -> ToolExecution | None:
    durable_key = hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
    async with session_factory() as session:
        return await session.scalar(
            select(ToolExecution).where(ToolExecution.idempotency_key == durable_key)
        )


async def test_email_update_fills_blank_field_and_commits_claim_before_patch(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(
        session_factory,
        provider,
        phone=phone,
        email=None,
    )
    request = _request(contact_id, conversation_id, email="trusted@example.test")

    async def inspect_claim() -> None:
        execution = await _execution(session_factory, request)
        assert execution is not None
        assert execution.status is ToolExecutionStatus.STARTED
        assert execution.sanitized_result is None

    provider.before_patch = inspect_claim

    result = await _update(session_factory, request, provider)
    execution = await _execution(session_factory, request)

    assert result.outcome is CRMContactUpdateOutcome.UPDATED
    assert result.updated_fields == ("email",)
    assert provider.patch_count == 1
    assert provider.patch_payloads == [{"properties": {"email": "trusted@example.test"}}]
    assert (
        "PATCH",
        f"/crm/objects/2026-09/contacts/{provider.contact_id}",
    ) in provider.requests
    assert all(
        set(body["properties"]).issubset({"email", "phone"}) for body in provider.patch_payloads
    )
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
        link = await session.get(CRMContactLink, contact_id)
    assert contact is not None and contact.email == "trusted@example.test"
    assert link is not None and link.linked_by is CRMContactLinkProvenance.PHONE_E164


@pytest.mark.parametrize(
    ("provider_email", "provider_phone", "selected_email", "selected_phone", "expected"),
    [
        (None, None, "selected@example.test", None, {"email": "selected@example.test"}),
        (
            "selected@example.test",
            None,
            "selected@example.test",
            None,
            None,
        ),
        ("staff@example.test", None, "selected@example.test", None, "conflict"),
        (None, None, None, "local", {"phone": "local"}),
        (None, "local_national", None, "local", None),
        (None, "+966551234567", None, "local", "conflict"),
        (
            "selected@example.test",
            "local_national",
            "selected@example.test",
            "local",
            None,
        ),
        (
            "selected@example.test",
            None,
            "selected@example.test",
            "local",
            {"phone": "local"},
        ),
    ],
)
async def test_provider_field_ownership_and_atomic_patch_selection(
    session_factory,
    provider_email,
    provider_phone,
    selected_email,
    selected_phone,
    expected,
) -> None:
    local_phone = _phone()
    if selected_phone == "local":
        selected_phone = local_phone
    if provider_phone == "local_national":
        provider_phone = f"0{local_phone[-9:]}"
    provider = MockHubSpotUpdate(email=provider_email, phone=local_phone)
    contact_id, conversation_id = await _owner(
        session_factory,
        provider,
        phone=local_phone,
        email=selected_email,
        linked_by=CRMContactLinkProvenance.PHONE_E164,
    )
    props = provider.contact_body["properties"]
    assert isinstance(props, dict)
    props["phone"] = provider_phone
    props["mobilephone"] = local_phone
    if isinstance(expected, dict) and expected.get("phone") == "local":
        expected = {"phone": local_phone}
    if provider_email is not None:
        provider.email_records[provider_email.casefold()] = provider.contact_body
    request = _request(
        contact_id,
        conversation_id,
        email=selected_email,
        phone=selected_phone,
    )

    result = await _update(session_factory, request, provider)

    if expected == "conflict":
        assert result.outcome is CRMContactUpdateOutcome.CONFLICT
        assert provider.patch_count == 0
    elif expected is None:
        assert result.outcome is CRMContactUpdateOutcome.UNCHANGED
        assert provider.patch_count == 0
    else:
        assert result.outcome is CRMContactUpdateOutcome.UPDATED
        assert provider.patch_count == 1
        assert provider.patch_payloads == [{"properties": expected}]
        assert set(provider.patch_payloads[0]["properties"]).issubset({"email", "phone"})


async def test_two_selected_fields_conflict_without_partial_patch(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(email="staff@example.test", phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(
        contact_id,
        conversation_id,
        email="selected@example.test",
        phone=phone,
    )

    result = await _update(session_factory, request, provider)

    assert result.outcome is CRMContactUpdateOutcome.CONFLICT
    assert provider.patch_count == 0


async def test_both_blank_selected_fields_are_filled_in_one_patch(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=None)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    props = provider.contact_body["properties"]
    assert isinstance(props, dict)
    props["mobilephone"] = phone
    request = _request(
        contact_id,
        conversation_id,
        email="both@example.test",
        phone=phone,
    )

    result = await _update(session_factory, request, provider)

    assert result.outcome is CRMContactUpdateOutcome.UPDATED
    assert result.updated_fields == ("email", "phone")
    assert provider.patch_payloads == [
        {"properties": {"email": "both@example.test", "phone": phone}}
    ]


async def test_local_identity_conflict_and_incomplete_provider_search_block_patch(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(
        session_factory, provider, phone=phone, email="local@example.test"
    )
    conflicting_request = _request(contact_id, conversation_id, email="different@example.test")

    local_conflict = await _update(session_factory, conflicting_request, provider)

    assert local_conflict.outcome is CRMContactUpdateOutcome.CONFLICT
    assert provider.requests == []

    phone_b = _phone()
    provider_b = MockHubSpotUpdate(phone=phone_b)
    contact_b, conversation_b = await _owner(session_factory, provider_b, phone=phone_b, email=None)
    provider_b.search_body = {
        "results": [provider_b.contact_body],
        "total": 2,
        "paging": {"next": {"after": "10"}},
    }
    incomplete = await _update(
        session_factory,
        _request(contact_b, conversation_b, email="new@example.test"),
        provider_b,
    )

    assert incomplete.outcome is CRMContactUpdateOutcome.CONFLICT
    assert incomplete.error_code is not None
    assert incomplete.error_code.value == "ambiguous_identity"
    assert provider_b.patch_count == 0


async def test_email_link_reverifies_and_never_writes_mobile_or_other_properties(
    session_factory,
) -> None:
    email = "verified@example.test"
    phone = _phone()
    provider = MockHubSpotUpdate(email=email, phone=None)
    contact_id, conversation_id = await _owner(
        session_factory,
        provider,
        phone=None,
        email=email,
        linked_by=CRMContactLinkProvenance.VERIFIED_EMAIL,
    )
    request = _request(contact_id, conversation_id, phone=phone)

    result = await _update(session_factory, request, provider)

    assert result.outcome is CRMContactUpdateOutcome.UPDATED
    assert provider.patch_payloads == [{"properties": {"phone": phone}}]
    assert "mobilephone" not in json.dumps(provider.patch_payloads)
    assert provider.contact_body["properties"]["email"] == email


async def test_no_active_link_wrong_portal_and_stale_link_never_patch(session_factory) -> None:
    provider = MockHubSpotUpdate(phone=_phone())
    phone = provider.contact_body["properties"]["phone"]
    contact_id, conversation_id = await _owner(
        session_factory, provider, phone=phone, email=None, create_link=False
    )
    request = _request(contact_id, conversation_id, email="new@example.test")
    missing = await _update(session_factory, request, provider)
    assert missing.outcome is CRMContactUpdateOutcome.CONFLICT
    assert provider.requests == []

    stale_provider = MockHubSpotUpdate(phone=_phone())
    stale_phone = stale_provider.contact_body["properties"]["phone"]
    stale_contact, stale_conversation = await _owner(
        session_factory,
        stale_provider,
        phone=stale_phone,
        email=None,
        state=CRMContactLinkState.STALE,
    )
    stale_result = await _update(
        session_factory,
        _request(stale_contact, stale_conversation, email="new@example.test"),
        stale_provider,
    )
    assert stale_result.outcome is CRMContactUpdateOutcome.CONFLICT
    assert stale_provider.requests == []

    wrong_portal_provider = MockHubSpotUpdate(phone=_phone())
    wrong_phone = wrong_portal_provider.contact_body["properties"]["phone"]
    wrong_contact, wrong_conversation = await _owner(
        session_factory, wrong_portal_provider, phone=wrong_phone, email=None
    )
    wrong = await _update(
        session_factory,
        _request(wrong_contact, wrong_conversation, email="new@example.test"),
        wrong_portal_provider,
        settings=_settings(portal_id="999999"),
    )
    assert wrong.outcome is CRMContactUpdateOutcome.NOT_CONFIGURED
    assert wrong_portal_provider.requests == []


async def test_wrong_account_and_unverified_production_capability_block_patch(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="new@example.test")
    provider.portal_id = 444444

    mismatch = await _update(session_factory, request, provider)

    assert mismatch.outcome is CRMContactUpdateOutcome.NOT_CONFIGURED
    assert provider.patch_count == 0

    production_provider = MockHubSpotUpdate(phone=phone)
    prod_result = await _update(
        session_factory,
        request.model_copy(update={"idempotency_key": str(uuid.uuid4())}),
        production_provider,
        settings=_settings(app_env="production"),
    )
    assert prod_result.outcome is CRMContactUpdateOutcome.NOT_CONFIGURED
    assert prod_result.error_code is not None
    assert production_provider.patch_count == 0


async def test_provider_404_or_archived_contact_marks_link_stale_without_patch(
    session_factory,
) -> None:
    for archived, status in ((False, 404), (True, 200)):
        phone = _phone()
        provider = MockHubSpotUpdate(phone=phone)
        provider.get_status = status
        provider.contact_body["archived"] = archived
        contact_id, conversation_id = await _owner(
            session_factory, provider, phone=phone, email=None
        )

        result = await _update(
            session_factory,
            _request(contact_id, conversation_id, email="new@example.test"),
            provider,
        )

        assert result.outcome is CRMContactUpdateOutcome.CONFLICT
        assert provider.patch_count == 0
        async with session_factory() as session:
            link = await session.get(CRMContactLink, contact_id)
        assert link is not None and link.state is CRMContactLinkState.STALE


@pytest.mark.parametrize(
    ("status", "outcome", "error_code", "execution_status"),
    [
        (400, CRMContactUpdateOutcome.FAILED, "provider_validation", ToolExecutionStatus.FAILED),
        (401, CRMContactUpdateOutcome.FAILED, "permission_denied", ToolExecutionStatus.FAILED),
        (403, CRMContactUpdateOutcome.FAILED, "permission_denied", ToolExecutionStatus.FAILED),
        (409, CRMContactUpdateOutcome.CONFLICT, "identity_conflict", ToolExecutionStatus.REJECTED),
        (429, CRMContactUpdateOutcome.FAILED, "rate_limited", ToolExecutionStatus.FAILED),
        (
            500,
            CRMContactUpdateOutcome.UNCERTAIN,
            "provider_unavailable",
            ToolExecutionStatus.STARTED,
        ),
    ],
)
async def test_patch_status_failures_are_finite_and_do_not_leak_provider_body(
    session_factory, status, outcome, error_code, execution_status
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.patch_status = status
    provider.apply_patch = status >= 500
    provider.patch_body = {"message": "PRIVATE_PROVIDER_SENTINEL"}
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="private@example.test")

    result = await _update(session_factory, request, provider)
    execution = await _execution(session_factory, request)
    async with session_factory() as session:
        audit_rows = list(
            (
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == "update_crm_contact")
                )
            ).all()
        )

    assert result.outcome is outcome
    assert result.error_code.value == error_code
    assert execution is not None and execution.status is execution_status
    assert execution is not None
    durable_text = json.dumps(
        {
            "arguments": execution.sanitized_arguments,
            "result": execution.sanitized_result,
            "message": execution.safe_error_message,
            "audit": [row.sanitized_metadata for row in audit_rows],
        }
    )
    for sentinel in (
        "private@example.test",
        request.idempotency_key,
        MOCK_CREDENTIAL,
        "PRIVATE_PROVIDER_SENTINEL",
    ):
        assert sentinel not in durable_text


async def test_patch_timeout_is_uncertain_and_same_key_read_only_recovery_succeeds(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.patch_exception = httpx.ReadTimeout("private timeout body")
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="recover@example.test")

    uncertain = await _update(session_factory, request, provider)
    recovered = await _update(session_factory, request, provider)

    assert uncertain.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert recovered.outcome is CRMContactUpdateOutcome.UPDATED
    assert recovered.replayed is True
    assert provider.patch_count == 1


async def test_uncertain_patch_with_staff_conflict_is_not_retried(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.patch_exception = httpx.ReadTimeout("timeout")
    provider.apply_patch = False
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="trusted@example.test")
    first = await _update(session_factory, request, provider)
    properties = provider.contact_body["properties"]
    assert isinstance(properties, dict)
    properties["email"] = "staff@example.test"

    second = await _update(session_factory, request, provider)

    assert first.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert second.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert second.next_action is not None
    assert provider.patch_count == 1


async def test_same_key_success_replay_and_changed_evidence_conflict_without_provider_call(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="trusted@example.test")
    first = await _update(session_factory, request, provider)
    request_count = len(provider.requests)

    replay = await _update(session_factory, request, provider)
    conflict = await _update(
        session_factory,
        _request(
            contact_id,
            conversation_id,
            email="different@example.test",
            key=request.idempotency_key,
        ),
        provider,
    )

    assert first.outcome is CRMContactUpdateOutcome.UPDATED
    assert replay.outcome is CRMContactUpdateOutcome.UPDATED and replay.replayed
    assert conflict.error_code is not None
    assert conflict.error_code.value == "idempotency_conflict"
    assert provider.patch_count == 1
    assert len(provider.requests) == request_count


async def test_different_key_is_blocked_while_update_is_in_flight(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    first_request = _request(contact_id, conversation_id, email="first@example.test")
    second_request = _request(contact_id, conversation_id, email="first@example.test")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_claim() -> None:
        entered.set()
        await release.wait()

    provider.before_patch = pause_after_claim
    first_task = asyncio.create_task(_update(session_factory, first_request, provider))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = await asyncio.wait_for(_update(session_factory, second_request, provider), timeout=3)
    release.set()
    first = await asyncio.wait_for(first_task, timeout=3)

    assert first.outcome is CRMContactUpdateOutcome.UPDATED
    assert second.error_code is not None
    assert second.error_code.value == "operation_in_progress"
    assert provider.patch_count == 1


async def test_uncertain_create_blocks_contact_update(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    async with session_factory.begin() as session:
        session.add(
            ToolExecution(
                conversation_id=conversation_id,
                tool_name="create_crm_contact",
                status=ToolExecutionStatus.STARTED,
                idempotency_key=hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest(),
                sanitized_arguments={"operation": "create_crm_contact"},
            )
        )

    result = await _update(
        session_factory,
        _request(contact_id, conversation_id, email="new@example.test"),
        provider,
    )

    assert result.error_code is not None
    assert result.error_code.value == "operation_in_progress"
    assert provider.requests == []


async def test_local_identity_change_during_provider_read_blocks_patch(session_factory) -> None:
    original_phone = _phone()
    provider = MockHubSpotUpdate(phone=original_phone)
    contact_id, conversation_id = await _owner(
        session_factory, provider, phone=original_phone, email=None
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_first_read(_request: httpx.Request) -> httpx.Response:
        if provider.direct_read_count == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json=provider.contact_body)

    provider.contact_read_handler = pause_first_read
    request = _request(contact_id, conversation_id, email="new@example.test")
    task = asyncio.create_task(_update(session_factory, request, provider))
    await asyncio.wait_for(entered.wait(), timeout=3)
    async with session_factory.begin() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        contact.phone_e164 = _phone()
    release.set()
    result = await asyncio.wait_for(task, timeout=3)

    assert result.outcome is CRMContactUpdateOutcome.CONFLICT
    assert result.error_code is not None
    assert result.error_code.value == "local_state_changed"
    assert provider.patch_count == 0


async def test_mapping_change_during_provider_read_blocks_patch(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_read(_request: httpx.Request) -> httpx.Response:
        if provider.direct_read_count == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json=provider.contact_body)

    provider.contact_read_handler = pause_read
    task = asyncio.create_task(
        _update(
            session_factory,
            _request(contact_id, conversation_id, email="new@example.test"),
            provider,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=3)
    async with session_factory.begin() as session:
        link = await session.get(CRMContactLink, contact_id)
        assert link is not None
        link.state = CRMContactLinkState.STALE
    release.set()
    result = await asyncio.wait_for(task, timeout=3)

    assert result.outcome is CRMContactUpdateOutcome.CONFLICT
    assert provider.patch_count == 0


async def test_accepted_malformed_patch_response_reconciles_with_read_only_get(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.patch_content = b"{malformed response"
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="recover@example.test")

    result = await _update(session_factory, request, provider)

    assert result.outcome is CRMContactUpdateOutcome.UPDATED
    assert provider.patch_count == 1
    assert provider.direct_read_count >= 2


async def test_post_patch_verification_read_failure_remains_uncertain(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.search_error_after_patch = 503
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="verified-after-read@example.test")

    first = await _update(session_factory, request, provider)
    execution = await _execution(session_factory, request)
    provider.search_error_after_patch = None
    second = await _update(session_factory, request, provider)

    assert first.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert second.outcome is CRMContactUpdateOutcome.UPDATED
    assert second.replayed
    assert provider.patch_count == 1


async def test_same_key_in_flight_recovery_never_issues_second_patch(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="same@example.test")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause() -> None:
        entered.set()
        await release.wait()

    provider.before_patch = pause
    original = asyncio.create_task(_update(session_factory, request, provider))
    await asyncio.wait_for(entered.wait(), timeout=3)
    concurrent = await asyncio.wait_for(_update(session_factory, request, provider), timeout=3)
    release.set()
    completed = await asyncio.wait_for(original, timeout=3)

    assert concurrent.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert completed.outcome is CRMContactUpdateOutcome.UPDATED
    assert provider.patch_count == 1
    execution = await _execution(session_factory, request)
    assert execution is not None and execution.status is ToolExecutionStatus.SUCCEEDED


async def test_staff_edit_between_initial_read_and_patch_prevents_patch(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)

    async def edit_before_final_read(_request: httpx.Request) -> httpx.Response:
        if provider.direct_read_count == 2:
            props = provider.contact_body["properties"]
            assert isinstance(props, dict)
            props["email"] = "staff-edit@example.test"
        return httpx.Response(200, json=provider.contact_body)

    provider.contact_read_handler = edit_before_final_read
    result = await _update(
        session_factory,
        _request(contact_id, conversation_id, email="trusted@example.test"),
        provider,
    )

    assert result.outcome is CRMContactUpdateOutcome.CONFLICT
    assert provider.patch_count == 0


async def test_provider_read_permission_failure_does_not_patch(session_factory) -> None:
    for status in (401, 403):
        phone = _phone()
        provider = MockHubSpotUpdate(phone=phone)
        provider.get_status = status
        contact_id, conversation_id = await _owner(
            session_factory, provider, phone=phone, email=None
        )
        result = await _update(
            session_factory,
            _request(contact_id, conversation_id, email="trusted@example.test"),
            provider,
        )
        assert result.outcome is CRMContactUpdateOutcome.FAILED
        assert result.error_code is not None
        assert result.error_code.value == "permission_denied"
        assert provider.patch_count == 0


async def test_cross_contact_conversation_ownership_rejected_before_provider_access(
    session_factory,
) -> None:
    phone_a = _phone()
    provider_a = MockHubSpotUpdate(phone=phone_a)
    contact_a, _conversation_a = await _owner(
        session_factory, provider_a, phone=phone_a, email=None
    )
    phone_b = _phone()
    provider_b = MockHubSpotUpdate(phone=phone_b)
    _contact_b, conversation_b = await _owner(
        session_factory, provider_b, phone=phone_b, email=None
    )

    result = await _update(
        session_factory,
        _request(contact_a, conversation_b, email="trusted@example.test"),
        provider_a,
    )

    assert result.outcome is CRMContactUpdateOutcome.OWNERSHIP_REJECTED
    assert provider_a.requests == []


async def test_tool_execution_contains_only_hmac_tags_and_finite_result(session_factory) -> None:
    phone = _phone()
    email = "pii.sentinel@example.test"
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email=email, phone=phone)

    result = await _update(session_factory, request, provider)
    execution = await _execution(session_factory, request)
    async with session_factory() as session:
        audit = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "update_crm_contact")
            .order_by(AuditEvent.occurred_at.desc())
        )

    assert result.outcome is CRMContactUpdateOutcome.UPDATED
    assert execution is not None and audit is not None
    safe_text = json.dumps(
        {
            "arguments": execution.sanitized_arguments,
            "result": execution.sanitized_result,
            "error": execution.safe_error_message,
            "audit": audit.sanitized_metadata,
            "returned": result.model_dump(mode="json"),
        }
    )
    for sentinel in (email, phone, request.idempotency_key, MOCK_CREDENTIAL):
        assert sentinel not in safe_text
    assert execution.sanitized_arguments["email_identity_tag"]
    assert execution.sanitized_arguments["phone_identity_tag"]


async def test_test_environment_without_mock_transport_makes_no_network_request(
    session_factory,
) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    from receptionist.application.crm_contact_update import update_crm_contact

    result = await update_crm_contact(
        session_factory,
        _request(contact_id, conversation_id, email="trusted@example.test"),
        _settings(),
    )

    assert result.outcome is CRMContactUpdateOutcome.FAILED
    assert provider.requests == []


async def test_local_terminalization_failure_recovers_after_provider_success(
    session_factory, monkeypatch
) -> None:
    from receptionist.application import crm_contact_update as crm_update

    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="persist@example.test")
    original_terminalize = crm_update._terminalize

    def fail_success_terminalization(session, execution, snapshot, result) -> None:
        if result.outcome is CRMContactUpdateOutcome.UPDATED:
            raise SQLAlchemyError("simulated local terminalization failure")
        original_terminalize(session, execution, snapshot, result)

    with monkeypatch.context() as patcher:
        patcher.setattr(crm_update, "_terminalize", fail_success_terminalization)
        first = await _update(session_factory, request, provider)

    execution = await _execution(session_factory, request)
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)
    assert first.outcome is CRMContactUpdateOutcome.UNCERTAIN
    assert execution is not None and execution.status is ToolExecutionStatus.STARTED
    assert contact is not None and contact.email is None
    recovered = await _update(session_factory, request, provider)
    async with session_factory() as session:
        contact = await session.get(Contact, contact_id)

    assert recovered.outcome is CRMContactUpdateOutcome.UPDATED
    assert recovered.replayed
    assert provider.patch_count == 1
    assert contact is not None and contact.email == "persist@example.test"


async def test_definite_rate_limit_can_retry_only_on_explicit_same_key(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    provider.patch_status = 429
    provider.apply_patch = False
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="retry@example.test")

    first = await _update(session_factory, request, provider)
    provider.patch_status = 200
    provider.apply_patch = True
    second = await _update(session_factory, request, provider)

    assert first.outcome is CRMContactUpdateOutcome.FAILED
    assert first.next_action.value == "retry_same_key"
    assert second.outcome is CRMContactUpdateOutcome.UPDATED
    assert provider.patch_count == 2


async def test_same_key_field_presence_change_is_idempotency_conflict(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="selected@example.test")
    await _update(session_factory, request, provider)
    count = len(provider.requests)
    changed_fields = _request(
        contact_id,
        conversation_id,
        phone=phone,
        key=request.idempotency_key,
    )

    result = await _update(session_factory, changed_fields, provider)

    assert result.error_code is not None
    assert result.error_code.value == "idempotency_conflict"
    assert provider.patch_count == 1
    assert len(provider.requests) == count


async def test_same_key_cannot_be_reused_for_another_contact_conversation_or_operation(
    session_factory,
) -> None:
    phone_a = _phone()
    provider_a = MockHubSpotUpdate(phone=phone_a)
    contact_a, conversation_a = await _owner(session_factory, provider_a, phone=phone_a, email=None)
    request_a = _request(contact_a, conversation_a, email="owner@example.test")
    await _update(session_factory, request_a, provider_a)
    provider_requests = len(provider_a.requests)

    async with session_factory.begin() as session:
        alternate_conversation = Conversation(
            contact_id=contact_a,
            channel=ConversationChannel.PHONE,
            status=ConversationStatus.OPEN,
            control_mode=ControlMode.AI,
            language_mode=ConversationLanguageMode.UNKNOWN,
        )
        session.add(alternate_conversation)
    result_other_conversation = await _update(
        session_factory,
        _request(
            contact_a,
            alternate_conversation.id,
            email="owner@example.test",
            key=request_a.idempotency_key,
        ),
        provider_a,
    )

    phone_b = _phone()
    provider_b = MockHubSpotUpdate(phone=phone_b)
    contact_b, conversation_b = await _owner(session_factory, provider_b, phone=phone_b, email=None)
    result_other_contact = await _update(
        session_factory,
        _request(
            contact_b,
            conversation_b,
            email="owner@example.test",
            key=request_a.idempotency_key,
        ),
        provider_b,
    )

    assert result_other_conversation.error_code is not None
    assert result_other_conversation.error_code.value == "idempotency_conflict"
    assert result_other_contact.error_code is not None
    assert result_other_contact.error_code.value == "idempotency_conflict"
    assert len(provider_a.requests) == provider_requests
    assert provider_b.requests == []


async def test_same_key_cannot_be_reused_across_crm_operations(session_factory) -> None:
    phone = _phone()
    provider = MockHubSpotUpdate(phone=phone)
    contact_id, conversation_id = await _owner(session_factory, provider, phone=phone, email=None)
    request = _request(contact_id, conversation_id, email="owner@example.test")
    async with session_factory.begin() as session:
        session.add(
            ToolExecution(
                conversation_id=conversation_id,
                tool_name="create_crm_contact",
                status=ToolExecutionStatus.STARTED,
                idempotency_key=hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest(),
                sanitized_arguments={"operation": "create_crm_contact"},
            )
        )

    result = await _update(session_factory, request, provider)

    assert result.error_code is not None
    assert result.error_code.value == "idempotency_conflict"
    assert provider.requests == []

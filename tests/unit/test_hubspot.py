from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from receptionist.core.config import Settings
from receptionist.integrations.hubspot import (
    HubSpotClient,
    HubSpotErrorCode,
    HubSpotProviderError,
)

TEST_DATABASE_URL = "postgresql+psycopg://receptionist@localhost/receptionist_test"
AUTH_VALUE = "mock-bearer-value"
pytestmark = pytest.mark.unit


def settings(*, app_env: str = "test", configured: bool = True) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": app_env,
        "log_level": "INFO",
        "database_url": TEST_DATABASE_URL,
    }
    if configured:
        values.update(
            hubspot_service_key=AUTH_VALUE,
            hubspot_portal_id="123456",
            hubspot_request_timeout_seconds=2.0,
        )
    return Settings(**values)


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def contact_payload(
    *,
    record_id: str = "987654",
    email: str | None = "customer@example.com",
    phone: str | None = "+966501234567",
    mobilephone: str | None = None,
    archived: bool = False,
) -> dict[str, object]:
    return {
        "id": record_id,
        "properties": {"email": email, "phone": phone, "mobilephone": mobilephone},
        "createdAt": "2026-10-25T10:00:00Z",
        "updatedAt": "2026-10-25T10:00:00Z",
        "archived": archived,
    }


@pytest.mark.parametrize("app_env", ["test", "ci"])
@pytest.mark.asyncio
async def test_automated_environment_requires_an_explicit_mock_transport(app_env: str) -> None:
    with pytest.raises(HubSpotProviderError) as exc_info:
        HubSpotClient(settings(app_env=app_env))

    assert exc_info.value.code is HubSpotErrorCode.LIVE_TRANSPORT_DISABLED

    with pytest.raises(HubSpotProviderError) as live_transport_error:
        HubSpotClient(settings(app_env=app_env), transport=httpx.AsyncHTTPTransport())

    assert live_transport_error.value.code is HubSpotErrorCode.LIVE_TRANSPORT_DISABLED


@pytest.mark.asyncio
async def test_contact_reads_use_fixed_https_bearer_and_allowlisted_properties() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(contact_payload())

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        by_id = await client.get_contact_by_id("987654")
        by_email = await client.get_contact_by_email("customer@example.com")

    assert by_id.id == "987654"
    assert by_email.email == "customer@example.com"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/crm/objects/2026-09/contacts/987654"),
        ("GET", "/crm/objects/2026-09/contacts/customer@example.com"),
    ]
    for request in requests:
        assert request.url.scheme == "https"
        assert request.url.host == "api.hubapi.com"
        assert request.headers["Authorization"] == f"Bearer {AUTH_VALUE}"
        assert request.url.params["properties"] == "email,phone,mobilephone"
        assert AUTH_VALUE not in request.url.query.decode()
    assert requests[1].url.params["idProperty"] == "email"


@pytest.mark.asyncio
async def test_contact_search_is_bounded_and_reports_incomplete_pagination() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "total": 2,
                "results": [contact_payload()],
                "paging": {"next": {"after": "123456789"}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.search_contacts_by_phone("+966501234567")

    assert len(result.results) == 1
    assert result.has_more is True
    assert result.next_after == "123456789"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/crm/objects/2026-09/contacts/search"
    body = json.loads(requests[0].content)
    assert body == {
        "filterGroups": [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": "501234567"}]},
            {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": "501234567"}]},
        ],
        "properties": ["email", "phone", "mobilephone"],
        "limit": 20,
    }


@pytest.mark.asyncio
async def test_email_candidate_search_uses_exact_email_filter_and_one_page() -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return json_response({"total": 1, "results": [contact_payload()]})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.search_contacts_by_email("customer@example.com")

    assert result.complete is True
    assert seen == [
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": "customer@example.com",
                        }
                    ]
                }
            ],
            "properties": ["email", "phone", "mobilephone"],
            "limit": 20,
        }
    ]


@pytest.mark.asyncio
async def test_contact_create_and_update_send_only_allowed_nonblank_fields() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(contact_payload(email="new@example.com", phone=None))

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        await client.create_contact(email="new@example.com")
        await client.update_contact("987654", phone="+966501234567")

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/crm/objects/2026-09/contacts"),
        ("PATCH", "/crm/objects/2026-09/contacts/987654"),
    ]
    assert json.loads(requests[0].content) == {"properties": {"email": "new@example.com"}}
    assert json.loads(requests[1].content) == {"properties": {"phone": "+966501234567"}}
    assert all(AUTH_VALUE not in request.content.decode() for request in requests)


@pytest.mark.asyncio
async def test_contact_update_rejects_empty_or_unsupported_properties_before_request() -> None:
    async with HubSpotClient(
        settings(), transport=httpx.MockTransport(lambda request: json_response(contact_payload()))
    ) as client:
        with pytest.raises(HubSpotProviderError) as empty_error:
            await client.update_contact("987654")
        with pytest.raises(HubSpotProviderError) as blank_error:
            await client.update_contact("987654", email=" ")

    assert empty_error.value.code is HubSpotErrorCode.INVALID_REQUEST
    assert blank_error.value.code is HubSpotErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_note_create_uses_bounded_body_and_exact_contact_association() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "id": "654321",
                "properties": {
                    "hs_note_body": "Booking confirmed. source-op:abc123",
                    "hs_timestamp": "2026-10-25T10:30:00.000Z",
                },
                "archived": False,
                "associations": {"contacts": {"results": [{"id": "987654"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        note = await client.create_note(
            "987654",
            body="Booking confirmed. source-op:abc123",
            timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
        )

    assert note.id == "654321"
    assert note.contact_ids == ("987654",)
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/crm/objects/2026-09/notes"
    assert json.loads(requests[0].content) == {
        "properties": {
            "hs_timestamp": "2026-10-25T10:30:00.000Z",
            "hs_note_body": "Booking confirmed. source-op:abc123",
        },
        "associations": [
            {
                "to": {"id": "987654"},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_note_search_and_read_use_documented_routes_and_narrow_results() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search"):
            return json_response(
                {
                    "total": 1,
                    "results": [
                        {
                            "id": "654321",
                            "properties": {
                                "hs_note_body": "source-op:abc123",
                                "hs_timestamp": "2026-10-25T10:30:00.000Z",
                            },
                        }
                    ],
                }
            )
        return json_response(
            {
                "id": "654321",
                "properties": {
                    "hs_note_body": "source-op:abc123",
                    "hs_timestamp": "2026-10-25T10:30:00.000Z",
                },
                "associations": {"contacts": {"results": [{"id": "987654"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        found = await client.search_notes("source-op:abc123")
        note = await client.get_note("654321")

    assert found.complete is True
    assert found.results[0].id == "654321"
    assert note.contact_ids == ("987654",)
    assert requests[0].url.path == "/crm/objects/2026-09/notes/search"
    assert json.loads(requests[0].content) == {
        "query": "source-op:abc123",
        "properties": ["hs_note_body", "hs_timestamp"],
        "limit": 5,
    }
    assert requests[1].method == "GET"
    assert requests[1].url.path == "/crm/objects/2026-09/notes/654321"
    assert requests[1].url.params["associations"] == "contact"


@pytest.mark.asyncio
async def test_provider_response_extras_do_not_escape_narrow_contact_snapshot() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = contact_payload()
        payload["properties"] = {
            "email": "customer@example.com",
            "phone": "+966501234567",
            "mobilephone": None,
            "lifecyclestage": "customer",
            "firstname": "provider-only-name",
        }
        return json_response(payload)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        contact = await client.get_contact_by_id("987654")

    assert contact.id == "987654"
    assert not hasattr(contact, "firstname")
    assert "provider-only-name" not in repr(contact)


@pytest.mark.asyncio
async def test_missing_note_contact_association_is_repaired_and_read_back() -> None:
    requests: list[httpx.Request] = []
    note_payload: dict[str, object] = {
        "id": "654321",
        "properties": {"hs_note_body": "source-op:abc123", "hs_timestamp": "2026-10-25T10:30:00Z"},
        "associations": {"contacts": {"results": []}},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            note_payload["associations"] = {"contacts": {"results": [{"id": "987654"}]}}
            return httpx.Response(204)
        return json_response(note_payload)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        note = await client.ensure_note_contact_association("654321", "987654")

    assert note.contact_ids == ("987654",)
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/crm/objects/2026-09/notes/654321"),
        (
            "PUT",
            "/crm/objects/2026-09/notes/654321/associations/contact/987654/202",
        ),
        ("GET", "/crm/objects/2026-09/notes/654321"),
    ]


@pytest.mark.asyncio
async def test_association_repair_refuses_a_different_existing_contact() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "id": "654321",
                "properties": {
                    "hs_note_body": "source-op:abc123",
                    "hs_timestamp": "2026-10-25T10:30:00Z",
                },
                "associations": {"contacts": {"results": [{"id": "111111"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.ensure_note_contact_association("654321", "987654")

    assert exc_info.value.code is HubSpotErrorCode.CONFLICT
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("status", "code", "uncertain"),
    [
        (400, HubSpotErrorCode.VALIDATION, False),
        (401, HubSpotErrorCode.UNAUTHORIZED, False),
        (403, HubSpotErrorCode.PERMISSION_DENIED, False),
        (404, HubSpotErrorCode.NOT_FOUND, False),
        (409, HubSpotErrorCode.CONFLICT, False),
        (429, HubSpotErrorCode.RATE_LIMITED, False),
        (503, HubSpotErrorCode.PROVIDER_UNAVAILABLE, True),
    ],
)
@pytest.mark.asyncio
async def test_provider_status_errors_are_finite_and_do_not_expose_raw_body(
    status: int, code: HubSpotErrorCode, uncertain: bool
) -> None:
    body_sentinel = "provider-body-pii-secret-sentinel"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": body_sentinel, "detail": body_sentinel})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    error = exc_info.value
    assert error.code is code
    assert error.outcome_uncertain is uncertain
    assert error.status_code == status
    assert body_sentinel not in str(error)
    assert body_sentinel not in repr(error)
    assert error.__cause__ is None


@pytest.mark.parametrize("retry_after", ["99999", "Fri, 31 Dec 9999 23:59:59 GMT"])
@pytest.mark.asyncio
async def test_rate_limit_retry_after_is_bounded_and_never_retried_automatically(
    retry_after: str,
) -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429, headers={"Retry-After": retry_after}, json={"message": "private"}
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert request_count == 1
    assert exc_info.value.code is HubSpotErrorCode.RATE_LIMITED
    assert exc_info.value.retry_after_seconds == 300


@pytest.mark.parametrize("exception_class", [httpx.ReadTimeout, httpx.WriteTimeout])
@pytest.mark.asyncio
async def test_timeout_on_contact_create_is_uncertain_and_not_retried(
    exception_class: type[httpx.TimeoutException],
) -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise exception_class("private transport error", request=request)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert request_count == 1
    assert exc_info.value.code is HubSpotErrorCode.TIMEOUT
    assert exc_info.value.outcome_uncertain is True
    assert "private transport error" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_connect_timeout_is_not_misreported_as_successful_or_ambiguous_write() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("private connection error", request=request)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert exc_info.value.code is HubSpotErrorCode.TIMEOUT
    assert exc_info.value.outcome_uncertain is False


@pytest.mark.asyncio
async def test_oversized_and_unexpected_provider_responses_are_rejected() -> None:
    async def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * (1024 * 1024) + b"}")

    async with HubSpotClient(
        settings(), transport=httpx.MockTransport(oversized_handler)
    ) as client:
        with pytest.raises(HubSpotProviderError) as oversized_error:
            await client.get_contact_by_id("987654")
    assert oversized_error.value.code is HubSpotErrorCode.RESPONSE_TOO_LARGE

    async def malformed_handler(request: httpx.Request) -> httpx.Response:
        return json_response({"results": "not-a-list"})

    async with HubSpotClient(
        settings(), transport=httpx.MockTransport(malformed_handler)
    ) as client:
        with pytest.raises(HubSpotProviderError) as malformed_error:
            await client.get_contact_by_id("987654")
    assert malformed_error.value.code is HubSpotErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_missing_configuration_and_unsafe_record_ids_fail_before_http() -> None:
    with pytest.raises(HubSpotProviderError) as missing_error:
        HubSpotClient(
            settings(configured=False), transport=httpx.MockTransport(lambda req: json_response({}))
        )
    assert missing_error.value.code is HubSpotErrorCode.NOT_CONFIGURED

    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return json_response(contact_payload())

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as invalid_id:
            await client.get_contact_by_id("987654?redirect=attacker")
    assert invalid_id.value.code is HubSpotErrorCode.INVALID_REQUEST
    assert request_count == 0


@pytest.mark.parametrize(
    "partial_settings",
    [
        {"hubspot_service_key": AUTH_VALUE},
        {"hubspot_portal_id": "123456"},
    ],
    ids=["missing-portal-id", "missing-service-key"],
)
def test_incomplete_configuration_fails_before_client_creation(
    partial_settings: dict[str, str],
) -> None:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "local",
        "log_level": "INFO",
        "database_url": TEST_DATABASE_URL,
        **partial_settings,
    }
    incomplete_settings = Settings(**values)

    with pytest.raises(HubSpotProviderError) as exc_info:
        HubSpotClient(incomplete_settings)

    assert exc_info.value.code is HubSpotErrorCode.NOT_CONFIGURED

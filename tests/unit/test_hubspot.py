from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from receptionist.core.config import Settings
from receptionist.integrations.hubspot import (
    HubSpotCapabilityStatus,
    HubSpotClient,
    HubSpotErrorCode,
    HubSpotPreflightOutcome,
    HubSpotProviderError,
    preflight_hubspot,
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
                "archived": False,
                "properties": {
                    "hs_note_body": "Booking confirmed. source-op:abc123",
                    "hs_timestamp": "2026-10-25T10:30:00.000Z",
                },
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
            "hs_timestamp": "2026-10-25T10:30:00.000000Z",
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
async def test_note_create_preserves_historical_timestamp_microseconds() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "id": "654321",
                "archived": False,
                "properties": {
                    "hs_note_body": "source marker",
                    "hs_timestamp": "2026-10-25T10:30:00.123456Z",
                },
                "associations": {"contacts": {"results": [{"id": "987654"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        await client.create_note(
            "987654",
            body="source marker",
            timestamp=datetime(2026, 10, 25, 10, 30, 0, 123456, tzinfo=UTC),
        )

    assert json.loads(requests[0].content)["properties"]["hs_timestamp"] == (
        "2026-10-25T10:30:00.123456Z"
    )


@pytest.mark.asyncio
async def test_note_search_and_read_use_documented_routes_and_narrow_results() -> None:
    requests: list[httpx.Request] = []
    marker = "booking-execution:00000000-0000-4000-8000-000000000001"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search"):
            return json_response(
                {
                    "total": 1,
                    "results": [
                        {
                            "id": "654321",
                            "archived": False,
                            "properties": {
                                "hs_note_body": marker,
                                "hs_timestamp": "2026-10-25T10:30:00.000Z",
                            },
                        }
                    ],
                }
            )
        return json_response(
            {
                "id": "654321",
                "archived": False,
                "properties": {
                    "hs_note_body": marker,
                    "hs_timestamp": "2026-10-25T10:30:00.000Z",
                },
                "associations": {"contacts": {"results": [{"id": "987654"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        found = await client.search_notes(marker)
        note = await client.get_note("654321")

    assert found.complete is True
    assert found.results[0].id == "654321"
    assert note.contact_ids == ("987654",)
    assert requests[0].url.path == "/crm/objects/2026-09/notes/search"
    assert json.loads(requests[0].content) == {
        "query": marker,
        "properties": ["hs_note_body", "hs_timestamp"],
        "limit": 5,
    }
    assert requests[1].method == "GET"
    assert requests[1].url.path == "/crm/objects/2026-09/notes/654321"
    assert requests[1].url.params["associations"] == "contact"


@pytest.mark.parametrize(
    "marker",
    [
        "source-op:abc123",
        "booking-execution:not-a-uuid",
        "booking-execution:00000000-0000-4000-8000-000000000001-extra",
    ],
)
@pytest.mark.asyncio
async def test_note_search_accepts_only_the_deterministic_booking_marker(marker: str) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"total": 0, "results": []})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.search_notes(marker)

    assert exc_info.value.code is HubSpotErrorCode.INVALID_REQUEST
    assert requests == []


@pytest.mark.parametrize(
    ("include_associations", "associations", "expected_ids", "complete"),
    [
        (False, None, (), False),
        (True, {"contacts": {}}, (), False),
        (True, {"contacts": {"results": []}}, (), True),
        (
            True,
            {
                "contacts": {
                    "results": [{"id": "987654"}],
                    "paging": {"next": {"after": "next-page"}},
                }
            },
            ("987654",),
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_note_parser_distinguishes_association_evidence_completeness(
    include_associations: bool,
    associations: object,
    expected_ids: tuple[str, ...],
    complete: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = {
            "id": "654321",
            "archived": False,
            "properties": {"hs_note_body": "source marker", "hs_timestamp": "2026-10-25T10:30:00Z"},
        }
        if include_associations:
            payload["associations"] = associations
        return json_response(payload)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        note = await client.get_note("654321")

    assert note.contact_ids == expected_ids
    assert note.contact_associations_complete is complete


@pytest.mark.asyncio
async def test_malformed_note_association_payload_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "id": "654321",
                "archived": False,
                "properties": {
                    "hs_note_body": "source marker",
                    "hs_timestamp": "2026-10-25T10:30:00Z",
                },
                "associations": {"contacts": {"results": "not-a-list"}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.get_note("654321")

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_association_repair_refuses_missing_association_evidence() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "id": "654321",
                "archived": False,
                "properties": {
                    "hs_note_body": "source marker",
                    "hs_timestamp": "2026-10-25T10:30:00Z",
                },
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.ensure_note_contact_association(
                "654321",
                "987654",
                expected_body="source marker",
                expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True
    assert [request.method for request in requests] == ["GET"]


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
        "archived": False,
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
        note = await client.ensure_note_contact_association(
            "654321",
            "987654",
            expected_body="source-op:abc123",
            expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
        )

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
                "archived": False,
                "properties": {
                    "hs_note_body": "source-op:abc123",
                    "hs_timestamp": "2026-10-25T10:30:00Z",
                },
                "associations": {"contacts": {"results": [{"id": "111111"}]}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.ensure_note_contact_association(
                "654321",
                "987654",
                expected_body="source-op:abc123",
                expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

    assert exc_info.value.code is HubSpotErrorCode.CONFLICT
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("body", "timestamp", "archived"),
    [
        ("changed body", "2026-10-25T10:30:00Z", False),
        ("source-op:abc123", "2026-10-25T10:31:00Z", False),
        ("source-op:abc123", "2026-10-25T10:30:00Z", True),
    ],
)
@pytest.mark.asyncio
async def test_association_repair_rechecks_note_identity_before_put(
    body: str, timestamp: str, archived: bool
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "id": "654321",
                "properties": {"hs_note_body": body, "hs_timestamp": timestamp},
                "archived": archived,
                "associations": {"contacts": {"results": []}},
            }
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.ensure_note_contact_association(
                "654321",
                "987654",
                expected_body="source-op:abc123",
                expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

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
    assert oversized_error.value.outcome_uncertain is False

    async def malformed_handler(request: httpx.Request) -> httpx.Response:
        return json_response({"results": "not-a-list"})

    async with HubSpotClient(
        settings(), transport=httpx.MockTransport(malformed_handler)
    ) as client:
        with pytest.raises(HubSpotProviderError) as malformed_error:
            await client.get_contact_by_id("987654")
    assert malformed_error.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert malformed_error.value.outcome_uncertain is False


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


@pytest.mark.asyncio
async def test_contact_create_accepted_with_malformed_json_is_uncertain_and_redacted() -> None:
    body_sentinel = "accepted-write-response-private-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            content=f'{{"detail":"{body_sentinel} {AUTH_VALUE}"'.encode(),
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    error = exc_info.value
    assert error.code is HubSpotErrorCode.INVALID_RESPONSE
    assert error.outcome_uncertain is True
    assert body_sentinel not in str(error)
    assert body_sentinel not in repr(error)
    assert AUTH_VALUE not in str(error)
    assert AUTH_VALUE not in repr(error)
    assert error.__cause__ is None


@pytest.mark.parametrize("response_body", [b"[]", b""], ids=["json-array", "empty-body"])
@pytest.mark.asyncio
async def test_accepted_contact_create_with_non_object_or_empty_body_is_uncertain(
    response_body: bytes,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=response_body)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_contact_create_accepted_without_record_id_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"properties": {"email": "customer@example.com"}}, 201)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_contact_update_accepted_with_malformed_snapshot_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"id": "987654", "properties": "unexpected"}, 200)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.update_contact("987654", phone="+966501234567")

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_note_create_accepted_with_malformed_json_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"{malformed")

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_note(
                "987654",
                body="Booking confirmed. source-op:abc123",
                timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_note_create_accepted_without_note_id_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"properties": {"hs_note_body": "source-op:abc123", "hs_timestamp": "now"}}, 201
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_note(
                "987654",
                body="Booking confirmed. source-op:abc123",
                timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

    assert exc_info.value.code is HubSpotErrorCode.INVALID_RESPONSE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_accepted_contact_create_with_oversized_response_is_uncertain() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"{" + b" " * (1024 * 1024) + b"}")

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert exc_info.value.code is HubSpotErrorCode.RESPONSE_TOO_LARGE
    assert exc_info.value.outcome_uncertain is True


@pytest.mark.asyncio
async def test_malformed_read_response_remains_non_uncertain() -> None:
    body_sentinel = "read-response-private-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f'{{"detail":"{body_sentinel}"'.encode())

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.get_contact_by_id("987654")

    error = exc_info.value
    assert error.code is HubSpotErrorCode.INVALID_RESPONSE
    assert error.outcome_uncertain is False
    assert body_sentinel not in str(error)
    assert body_sentinel not in repr(error)


@pytest.mark.asyncio
async def test_association_write_with_unusable_readback_is_uncertain_without_second_put() -> None:
    requests: list[httpx.Request] = []
    response_body_sentinel = "association-readback-private-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(204)
        if (
            request.method == "GET"
            and len([item for item in requests if item.method == "GET"]) == 1
        ):
            return json_response(
                {
                    "id": "654321",
                    "archived": False,
                    "properties": {
                        "hs_note_body": "source-op:abc123",
                        "hs_timestamp": "2026-10-25T10:30:00Z",
                    },
                    "associations": {"contacts": {"results": []}},
                }
            )
        return httpx.Response(
            200,
            content=f'{{"detail":"{response_body_sentinel}"'.encode(),
        )

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.ensure_note_contact_association(
                "654321",
                "987654",
                expected_body="source-op:abc123",
                expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            )

    error = exc_info.value
    assert error.code is HubSpotErrorCode.INVALID_RESPONSE
    assert error.outcome_uncertain is True
    assert response_body_sentinel not in str(error)
    assert response_body_sentinel not in repr(error)
    assert AUTH_VALUE not in repr(error)
    assert sum(request.method == "PUT" for request in requests) == 1


@pytest.mark.asyncio
async def test_account_preflight_verifies_identity_but_not_write_capabilities() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"portalId": 123456})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is HubSpotPreflightOutcome.VERIFIED
    assert result.account_identity_verified is True
    assert result.capability_status is HubSpotCapabilityStatus.UNVERIFIED
    assert result.writes_enabled is False
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url == "https://api.hubapi.com/account-info/2026-03/details"
    assert requests[0].headers["Authorization"] == f"Bearer {AUTH_VALUE}"
    assert AUTH_VALUE not in requests[0].url.query.decode()


@pytest.mark.asyncio
async def test_account_preflight_rejects_portal_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"portalId": 777777})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH
    assert result.account_identity_verified is False
    assert result.writes_enabled is False


@pytest.mark.parametrize(
    "partial_settings",
    [
        {"hubspot_portal_id": "123456"},
        {"hubspot_service_key": AUTH_VALUE},
    ],
    ids=["missing-service-key", "missing-portal-id"],
)
@pytest.mark.asyncio
async def test_account_preflight_returns_not_configured_without_http(
    partial_settings: dict[str, str],
) -> None:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "INFO",
        "database_url": TEST_DATABASE_URL,
        **partial_settings,
    }
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"portalId": 123456})

    result = await preflight_hubspot(Settings(**values), transport=httpx.MockTransport(handler))

    assert result.outcome is HubSpotPreflightOutcome.NOT_CONFIGURED
    assert result.account_identity_verified is False
    assert result.writes_enabled is False
    assert requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"portalId": "123456"},
        {"portalId": ""},
        {"portalId": 0},
        {"portalId": True},
    ],
)
@pytest.mark.asyncio
async def test_account_preflight_rejects_malformed_portal_id(
    payload: dict[str, object],
) -> None:
    provider_sentinel = "provider-account-secret-sentinel"

    async def handler(request: httpx.Request) -> httpx.Response:
        return json_response({**payload, "private": provider_sentinel})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE
    assert result.account_identity_verified is False
    assert provider_sentinel not in repr(result)
    assert AUTH_VALUE not in repr(result)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, HubSpotPreflightOutcome.PERMISSION_DENIED),
        (403, HubSpotPreflightOutcome.PERMISSION_DENIED),
        (404, HubSpotPreflightOutcome.VERIFICATION_UNAVAILABLE),
        (429, HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE),
        (503, HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE),
    ],
)
@pytest.mark.asyncio
async def test_account_preflight_maps_safe_provider_failures(
    status: int, outcome: HubSpotPreflightOutcome
) -> None:
    provider_sentinel = "private-account-provider-response"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": provider_sentinel})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is outcome
    assert result.account_identity_verified is False
    assert result.writes_enabled is False
    assert provider_sentinel not in str(result)
    assert provider_sentinel not in repr(result)
    assert AUTH_VALUE not in repr(result)


@pytest.mark.asyncio
async def test_account_preflight_timeout_is_provider_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private account response timeout", request=request)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE
    assert result.writes_enabled is False


@pytest.mark.parametrize(
    ("body", "response_sentinel"),
    [
        (b"not-json", ""),
        (b"[]", ""),
        (b"", ""),
        (b'{"private":"raw-account-response-sentinel"}', "raw-account-response-sentinel"),
    ],
)
@pytest.mark.asyncio
async def test_account_preflight_rejects_unusable_success_response(
    body: bytes, response_sentinel: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()

    assert result.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE
    assert result.account_identity_verified is False
    if response_sentinel:
        assert response_sentinel not in repr(result)
    assert AUTH_VALUE not in repr(result)


@pytest.mark.parametrize("changed_setting", ["hubspot_service_key", "hubspot_portal_id"])
@pytest.mark.asyncio
async def test_configuration_change_invalidates_account_preflight(
    changed_setting: str,
) -> None:
    from pydantic import SecretStr

    configured = settings()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"portalId": 123456})

    async with HubSpotClient(configured, transport=httpx.MockTransport(handler)) as client:
        verified = await client.preflight_account()
        assert verified.outcome is HubSpotPreflightOutcome.VERIFIED

        if changed_setting == "hubspot_service_key":
            configured.hubspot_service_key = SecretStr("rotated-key")
        else:
            configured.hubspot_portal_id = "654321"
        invalidated = await client.preflight_account()

    assert invalidated.outcome is HubSpotPreflightOutcome.VERIFICATION_UNAVAILABLE
    assert invalidated.account_identity_verified is False
    assert invalidated.writes_enabled is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_mocked_account_match_never_enables_production_writes() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"portalId": 123456})

    verification_settings = settings(app_env="production")
    operational_settings = settings(app_env="production")
    async with HubSpotClient(
        verification_settings, transport=httpx.MockTransport(handler)
    ) as verifier:
        result = await verifier.preflight_account()
    assert result.outcome is HubSpotPreflightOutcome.VERIFIED
    assert result.writes_enabled is False

    async with HubSpotClient(
        operational_settings, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HubSpotProviderError) as exc_info:
            await client.create_contact(email="customer@example.com")

    assert exc_info.value.code is HubSpotErrorCode.ACTIVATION_REQUIRED
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_failed_preflight_blocks_contact_note_and_association_writes() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"portalId": 777777})

    async with HubSpotClient(settings(), transport=httpx.MockTransport(handler)) as client:
        result = await client.preflight_account()
        assert result.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH
        for write in (
            lambda: client.create_contact(email="customer@example.com"),
            lambda: client.update_contact("987654", email="customer@example.com"),
            lambda: client.create_note(
                "654321",
                body="Booking confirmed. source-op:abc123",
                timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            ),
            lambda: client.ensure_note_contact_association(
                "654321",
                "987654",
                expected_body="source-op:abc123",
                expected_timestamp=datetime(2026, 10, 25, 10, 30, tzinfo=UTC),
            ),
        ):
            with pytest.raises(HubSpotProviderError) as exc_info:
                await write()
            assert exc_info.value.code is HubSpotErrorCode.ACTIVATION_REQUIRED

    assert len(requests) == 1
    assert requests[0].method == "GET"

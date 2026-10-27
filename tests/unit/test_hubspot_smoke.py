from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.engine import URL

import scripts.hubspot_smoke as smoke
from receptionist.integrations.hubspot import HubSpotClient

pytestmark = pytest.mark.unit

PORTAL_ID = "123456"
_SMOKE_DB_CREDENTIAL = "smoke" + "_only"
AUTH_SENTINEL = "private-smoke-service-key"


def _database_url(
    *,
    user: str = "hubspot_smoke",
    password: str = _SMOKE_DB_CREDENTIAL,
    host: str = "localhost",
    port: int = 55433,
    database: str = "receptionist_hubspot_smoke",
) -> str:
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


DATABASE_URL = _database_url()


def _environment() -> dict[str, str]:
    return {
        "RECEPTIONIST_APP_ENV": "local",
        "RECEPTIONIST_DATABASE_URL": DATABASE_URL,
        "RECEPTIONIST_HUBSPOT_SERVICE_KEY": AUTH_SENTINEL,
        "RECEPTIONIST_HUBSPOT_PORTAL_ID": PORTAL_ID,
        "RECEPTIONIST_HUBSPOT_SMOKE_ENV": "developer_test",
        "RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM": smoke.SMOKE_CONFIRMATION,
        "RECEPTIONIST_HUBSPOT_SMOKE_CAPABILITIES_CONFIRM": (
            smoke.HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION
        ),
    }


def _configuration(monkeypatch) -> smoke.SmokeConfiguration:
    monkeypatch.setattr(smoke, "REVIEWED_DEVELOPER_PORTAL_IDS", frozenset({PORTAL_ID}))
    return smoke.build_smoke_configuration(_environment())


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM", "", "explicit smoke confirmation"),
        ("RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM", "YES", "explicit smoke confirmation"),
        ("RECEPTIONIST_HUBSPOT_SMOKE_ENV", "production", "developer test environment"),
        (
            "RECEPTIONIST_HUBSPOT_SMOKE_CAPABILITIES_CONFIRM",
            "",
            "capability review confirmation",
        ),
        ("RECEPTIONIST_HUBSPOT_PORTAL_ID", "654321", "reviewed developer portal"),
    ],
)
def test_smoke_guard_rejects_missing_opt_in_and_unreviewed_target(
    monkeypatch, name: str, value: str, expected: str
) -> None:
    monkeypatch.setattr(smoke, "REVIEWED_DEVELOPER_PORTAL_IDS", frozenset({PORTAL_ID}))
    environment = _environment()
    environment[name] = value

    with pytest.raises(smoke.SmokeGuardError, match=expected):
        smoke.build_smoke_configuration(environment)


@pytest.mark.parametrize(
    "database_url",
    [
        _database_url(port=55432, database="receptionist_test"),
        _database_url(user="receptionist"),
        _database_url(host="db.example"),
        _database_url(port=5432),
    ],
)
def test_smoke_guard_rejects_database_not_matching_dedicated_identity(
    monkeypatch, database_url: str
) -> None:
    monkeypatch.setattr(smoke, "REVIEWED_DEVELOPER_PORTAL_IDS", frozenset({PORTAL_ID}))
    environment = _environment()
    environment["RECEPTIONIST_DATABASE_URL"] = database_url

    with pytest.raises(smoke.SmokeGuardError, match="dedicated smoke database"):
        smoke.build_smoke_configuration(environment)


def test_smoke_guard_rejects_missing_credentials_and_caller_contact_data(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "REVIEWED_DEVELOPER_PORTAL_IDS", frozenset({PORTAL_ID}))
    no_key = _environment()
    no_key.pop("RECEPTIONIST_HUBSPOT_SERVICE_KEY")
    with pytest.raises(smoke.SmokeGuardError, match="HubSpot credentials"):
        smoke.build_smoke_configuration(no_key)

    caller_data = _environment()
    caller_data["RECEPTIONIST_HUBSPOT_SMOKE_EMAIL"] = "customer@example.com"
    with pytest.raises(smoke.SmokeGuardError, match="caller-supplied Contact data"):
        smoke.build_smoke_configuration(caller_data)


def test_smoke_guard_never_accepts_unconfigured_reviewed_portal(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "REVIEWED_DEVELOPER_PORTAL_IDS", frozenset())

    with pytest.raises(smoke.SmokeGuardError, match="no reviewed developer portal"):
        smoke.build_smoke_configuration(_environment())


def test_synthetic_contact_email_contains_only_run_ownership_marker() -> None:
    run_id = uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d")

    email = smoke._synthetic_contact_email(run_id)

    assert email == f"hubspot-smoke+{run_id}@example.com"
    assert smoke._parse_synthetic_contact_email(email) == run_id
    assert smoke._parse_synthetic_contact_email("customer@example.com") is None


def _mock_provider(
    configuration: smoke.SmokeConfiguration,
    *,
    account_portal: str = PORTAL_ID,
    timeout_after_create: bool = False,
) -> tuple[HubSpotClient, list[httpx.Request], dict[str, object]]:
    requests: list[httpx.Request] = []
    record: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/account-info/2026-03/details":
            return httpx.Response(200, json={"portalId": int(account_portal)})
        if request.method == "GET" and request.url.params.get("idProperty") == "email":
            if not record:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=record)
        if request.method == "POST" and request.url.path.endswith("/contacts"):
            payload = json.loads(request.content)
            record.update(
                {
                    "id": "987654321",
                    "properties": {
                        "email": payload["properties"]["email"],
                        "phone": None,
                        "mobilephone": None,
                    },
                    "archived": False,
                }
            )
            if timeout_after_create:
                raise httpx.ReadTimeout("private mock timeout")
            return httpx.Response(201, json=record)
        if request.method == "GET" and request.url.path.endswith("/contacts/987654321"):
            if not record:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=record)
        if request.method == "DELETE" and request.url.path.endswith("/contacts/987654321"):
            record["archived"] = True
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "unexpected request"})

    return (
        HubSpotClient(configuration.settings, transport=httpx.MockTransport(handler)),
        requests,
        record,
    )


@pytest.mark.asyncio
async def test_smoke_preflights_same_key_then_creates_verifies_and_cleans_exact_contact(
    monkeypatch,
) -> None:
    configuration = _configuration(monkeypatch)
    client, requests, record = _mock_provider(configuration)
    run_id = uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d")

    async with client:
        result = await smoke.run_hubspot_smoke_operations(configuration, client, run_id=run_id)

    assert result.outcome is smoke.SmokeOutcome.PASSED
    assert result.cleanup is smoke.SmokeCleanup.COMPLETE
    assert result.contact_id == "987654321"
    assert record["archived"] is True
    assert [request.url.path for request in requests] == [
        "/account-info/2026-03/details",
        "/crm/objects/2026-09/contacts/hubspot-smoke+e90c69bd-d7d5-4624-b55b-eefc175f3b1d@example.com",
        "/crm/objects/2026-09/contacts",
        "/crm/objects/2026-09/contacts/987654321",
        "/crm/objects/2026-09/contacts/987654321",
        "/crm/objects/2026-09/contacts/987654321",
    ]
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
        "DELETE",
    ]
    create_request = requests[2]
    assert create_request.headers["authorization"] == f"Bearer {AUTH_SENTINEL}"
    assert json.loads(create_request.content) == {
        "properties": {"email": "hubspot-smoke+e90c69bd-d7d5-4624-b55b-eefc175f3b1d@example.com"}
    }
    assert requests[-1].url.path == "/crm/objects/2026-09/contacts/987654321"
    assert AUTH_SENTINEL not in str(result)


@pytest.mark.asyncio
async def test_wrong_account_identity_prevents_contact_mutation(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    client, requests, _ = _mock_provider(configuration, account_portal="654321")

    async with client:
        result = await smoke.run_hubspot_smoke_operations(configuration, client)

    assert result.outcome is smoke.SmokeOutcome.NOT_RUN
    assert result.error_code is smoke.SmokeErrorCode.ACCOUNT_MISMATCH
    assert len(requests) == 1
    assert requests[0].url.path == "/account-info/2026-03/details"


@pytest.mark.asyncio
async def test_uncertain_contact_create_is_read_back_and_never_posted_twice(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    client, requests, record = _mock_provider(configuration, timeout_after_create=True)
    async with client:
        result = await smoke.run_hubspot_smoke_operations(
            configuration,
            client,
            run_id=uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d"),
        )

    assert result.outcome is smoke.SmokeOutcome.PASSED
    assert result.cleanup is smoke.SmokeCleanup.COMPLETE
    assert record["archived"] is True
    assert (
        sum(
            request.method == "POST" and request.url.path.endswith("/contacts")
            for request in requests
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cleanup_refuses_contact_belonging_to_another_run(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    client, requests, record = _mock_provider(configuration)
    foreign_email = smoke._synthetic_contact_email(uuid.uuid4())
    record.update(
        {
            "id": "987654321",
            "properties": {"email": foreign_email, "phone": None, "mobilephone": None},
            "archived": False,
        }
    )

    async with client:
        preflight = await client.preflight_account()
        client._activate_guarded_smoke_writes(
            portal_id=PORTAL_ID,
            capability_confirmation=smoke.HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
            contact_email=smoke._synthetic_contact_email(
                uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d")
            ),
        )
        with pytest.raises(smoke.HubSpotProviderError):
            await client.verify_smoke_contact(
                "987654321",
                expected_email=smoke._synthetic_contact_email(
                    uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d")
                ),
            )
        with pytest.raises(smoke.HubSpotProviderError):
            await client.archive_smoke_contact(
                "987654321",
                expected_email=smoke._synthetic_contact_email(
                    uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d")
                ),
            )

    assert preflight.account_identity_verified is True
    assert all(request.method != "DELETE" for request in requests)


@pytest.mark.asyncio
async def test_database_failure_prevents_constructing_a_provider_client(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    constructed = False

    class FailedDatabase:
        async def __aenter__(self):
            raise RuntimeError("private database error")

        async def __aexit__(self, *_args):
            return None

        async def dispose(self):
            return None

    class Resources:
        engine = FailedDatabase()

        async def dispose(self):
            return None

    def create_resources(_settings):
        return Resources()

    class ProviderMustNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(smoke, "create_database_resources", create_resources)
    monkeypatch.setattr(smoke, "HubSpotClient", ProviderMustNotBeConstructed)

    result = await smoke.run_smoke(configuration)

    assert result.outcome is smoke.SmokeOutcome.NOT_RUN
    assert result.error_code is smoke.SmokeErrorCode.DATABASE_UNAVAILABLE
    assert constructed is False


@pytest.mark.asyncio
async def test_database_runtime_identity_mismatch_prevents_provider_construction(
    monkeypatch,
) -> None:
    configuration = _configuration(monkeypatch)
    constructed = False

    class Connection:
        async def execute(self, _statement):
            return type(
                "Identity", (), {"one": lambda _self: ("receptionist_test", "receptionist", 5432)}
            )()

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            return None

    class Resources:
        engine = Engine()

    def create_resources(_settings):
        return Resources()

    class ProviderMustNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(smoke, "create_database_resources", create_resources)
    monkeypatch.setattr(smoke, "HubSpotClient", ProviderMustNotBeConstructed)

    result = await smoke.run_smoke(configuration)

    assert result.error_code is smoke.SmokeErrorCode.DATABASE_UNAVAILABLE
    assert constructed is False


@pytest.mark.asyncio
async def test_smoke_activation_is_limited_to_one_exact_synthetic_contact(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    client, requests, _ = _mock_provider(configuration)
    run_email = smoke._synthetic_contact_email(uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d"))

    async with client:
        preflight = await client.preflight_account()
        assert preflight.account_identity_verified
        client._activate_guarded_smoke_writes(
            portal_id=PORTAL_ID,
            capability_confirmation=smoke.HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
            contact_email=run_email,
        )
        await client.create_contact(email=run_email)
        with pytest.raises(smoke.HubSpotProviderError):
            await client.create_contact(email=run_email)
        with pytest.raises(smoke.HubSpotProviderError):
            await client.create_contact(email=smoke._synthetic_contact_email(uuid.uuid4()))
        with pytest.raises(smoke.HubSpotProviderError):
            await client.create_contact(email=run_email, phone="+14155552671")
        with pytest.raises(smoke.HubSpotProviderError):
            await client.update_contact("987654321", email="other@example.com")

    assert sum(request.method == "POST" for request in requests) == 1


@pytest.mark.parametrize("changed_setting", ["hubspot_service_key", "hubspot_portal_id"])
@pytest.mark.asyncio
async def test_smoke_activation_is_invalidated_by_key_rotation_or_portal_change(
    monkeypatch, changed_setting: str
) -> None:
    from pydantic import SecretStr

    configuration = _configuration(monkeypatch)
    client, requests, _ = _mock_provider(configuration)
    run_email = smoke._synthetic_contact_email(uuid.UUID("e90c69bd-d7d5-4624-b55b-eefc175f3b1d"))

    async with client:
        assert (await client.preflight_account()).account_identity_verified
        client._activate_guarded_smoke_writes(
            portal_id=PORTAL_ID,
            capability_confirmation=smoke.HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
            contact_email=run_email,
        )
        if changed_setting == "hubspot_service_key":
            configuration.settings.hubspot_service_key = SecretStr("rotated-service-key")
        else:
            configuration.settings.hubspot_portal_id = "654321"
        with pytest.raises(smoke.HubSpotProviderError):
            await client.create_contact(email=run_email)

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_production_client_cannot_enable_manual_smoke_writes(monkeypatch) -> None:
    configuration = _configuration(monkeypatch)
    production_settings = configuration.settings.model_copy(update={"app_env": "production"})
    client = HubSpotClient(
        production_settings,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )

    with pytest.raises(smoke.HubSpotProviderError):
        client._activate_guarded_smoke_writes(
            portal_id=PORTAL_ID,
            capability_confirmation=smoke.HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
            contact_email=smoke._synthetic_contact_email(uuid.uuid4()),
        )
    await client.aclose()

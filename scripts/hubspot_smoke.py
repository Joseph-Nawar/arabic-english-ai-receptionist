"""Guarded, manually invoked HubSpot developer-account Contact smoke."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from httpx import AsyncBaseTransport
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from receptionist.core.config import Settings
from receptionist.db.session import create_database_resources, dispose_database
from receptionist.integrations.hubspot import (
    HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
    HubSpotClient,
    HubSpotErrorCode,
    HubSpotPreflightOutcome,
    HubSpotProviderError,
)
from scripts.hubspot_smoke_allowlist import REVIEWED_DEVELOPER_PORTAL_IDS

SMOKE_ENVIRONMENT = "developer_test"
SMOKE_CONFIRMATION = "RUN_DEDICATED_HUBSPOT_DEVELOPER_TEST_SMOKE"
_SMOKE_DATABASE = "receptionist_hubspot_smoke"
_SMOKE_DATABASE_USER = "hubspot_smoke"
_SMOKE_DATABASE_PORT = 55433
_SMOKE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SMOKE_EMAIL_PATTERN = re.compile(
    r"^hubspot-smoke\+([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})@example\.com$"
)


class SmokeGuardErrorCode(StrEnum):
    INVALID_ENVIRONMENT = "invalid_environment"
    OPT_IN_REQUIRED = "opt_in_required"
    PORTAL_NOT_ALLOWLISTED = "portal_not_allowlisted"
    CAPABILITY_REVIEW_REQUIRED = "capability_review_required"
    UNSAFE_DATABASE = "unsafe_database"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    CALLER_DATA_REJECTED = "caller_data_rejected"


class SmokeOutcome(StrEnum):
    PASSED = "passed"
    NOT_RUN = "not_run"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SmokeCleanup(StrEnum):
    NOT_NEEDED = "not_needed"
    COMPLETE = "complete"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SmokeErrorCode(StrEnum):
    ACCOUNT_MISMATCH = "account_mismatch"
    CAPABILITY_REVIEW_REQUIRED = "capability_review_required"
    CONTACT_ALREADY_EXISTS = "contact_already_exists"
    CONTACT_IDENTITY_UNVERIFIED = "contact_identity_unverified"
    DATABASE_UNAVAILABLE = "database_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_UNCERTAIN = "cleanup_uncertain"


class SmokeGuardError(RuntimeError):
    """Safe local refusal raised before provider access."""

    def __init__(self, code: SmokeGuardErrorCode) -> None:
        self.code = code
        super().__init__(_SAFE_GUARD_MESSAGES[code])


_SAFE_GUARD_MESSAGES = {
    SmokeGuardErrorCode.INVALID_ENVIRONMENT: "smoke requires the local developer test environment",
    SmokeGuardErrorCode.OPT_IN_REQUIRED: "explicit smoke confirmation is required",
    SmokeGuardErrorCode.PORTAL_NOT_ALLOWLISTED: (
        "no reviewed developer portal allowlist accepts this portal"
    ),
    SmokeGuardErrorCode.CAPABILITY_REVIEW_REQUIRED: "capability review confirmation is required",
    SmokeGuardErrorCode.UNSAFE_DATABASE: "database must be the dedicated smoke database",
    SmokeGuardErrorCode.CREDENTIALS_UNAVAILABLE: (
        "HubSpot credentials or required smoke settings are unavailable"
    ),
    SmokeGuardErrorCode.CALLER_DATA_REJECTED: "caller-supplied Contact data is not allowed",
}


@dataclass(frozen=True, slots=True)
class SmokeConfiguration:
    settings: Settings
    portal_id: str


@dataclass(frozen=True, slots=True)
class SmokeRunResult:
    outcome: SmokeOutcome
    cleanup: SmokeCleanup
    error_code: SmokeErrorCode | None = None
    contact_id: str | None = None


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise SmokeGuardError(SmokeGuardErrorCode.CREDENTIALS_UNAVAILABLE)
    return value


def _assert_safe_smoke_database(database_url: str) -> None:
    try:
        parsed = make_url(database_url)
    except Exception:
        raise SmokeGuardError(SmokeGuardErrorCode.UNSAFE_DATABASE) from None
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host not in _SMOKE_DATABASE_HOSTS
        or parsed.port != _SMOKE_DATABASE_PORT
        or parsed.database != _SMOKE_DATABASE
        or parsed.username != _SMOKE_DATABASE_USER
        or not parsed.password
        or parsed.query
    ):
        raise SmokeGuardError(SmokeGuardErrorCode.UNSAFE_DATABASE)


def build_smoke_configuration(environment: Mapping[str, str]) -> SmokeConfiguration:
    """Validate every local guard before any provider client can be created."""
    if environment.get("RECEPTIONIST_APP_ENV", "").strip() != "local":
        raise SmokeGuardError(SmokeGuardErrorCode.INVALID_ENVIRONMENT)
    if environment.get("RECEPTIONIST_HUBSPOT_SMOKE_ENV", "").strip() != SMOKE_ENVIRONMENT:
        raise SmokeGuardError(SmokeGuardErrorCode.INVALID_ENVIRONMENT)
    if environment.get("RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM", "").strip() != SMOKE_CONFIRMATION:
        raise SmokeGuardError(SmokeGuardErrorCode.OPT_IN_REQUIRED)

    allowed_smoke_values = {
        "RECEPTIONIST_HUBSPOT_SMOKE_ENV",
        "RECEPTIONIST_HUBSPOT_SMOKE_CONFIRM",
        "RECEPTIONIST_HUBSPOT_SMOKE_CAPABILITIES_CONFIRM",
    }
    unsupported_smoke_fields = {
        name
        for name in environment
        if name.startswith("RECEPTIONIST_HUBSPOT_SMOKE_") and name not in allowed_smoke_values
    }
    if unsupported_smoke_fields:
        raise SmokeGuardError(SmokeGuardErrorCode.CALLER_DATA_REJECTED)

    portal_id = _required(environment, "RECEPTIONIST_HUBSPOT_PORTAL_ID")
    if portal_id not in REVIEWED_DEVELOPER_PORTAL_IDS:
        raise SmokeGuardError(SmokeGuardErrorCode.PORTAL_NOT_ALLOWLISTED)
    if (
        environment.get("RECEPTIONIST_HUBSPOT_SMOKE_CAPABILITIES_CONFIRM", "").strip()
        != HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION
    ):
        raise SmokeGuardError(SmokeGuardErrorCode.CAPABILITY_REVIEW_REQUIRED)

    database_url = _required(environment, "RECEPTIONIST_DATABASE_URL")
    _assert_safe_smoke_database(database_url)
    service_key = _required(environment, "RECEPTIONIST_HUBSPOT_SERVICE_KEY")
    try:
        settings = Settings(
            _env_file=None,
            app_env="local",
            log_level=environment.get("RECEPTIONIST_LOG_LEVEL", "INFO"),
            database_url=SecretStr(database_url),
            hubspot_service_key=SecretStr(service_key),
            hubspot_portal_id=portal_id,
            hubspot_request_timeout_seconds=float(
                environment.get("RECEPTIONIST_HUBSPOT_REQUEST_TIMEOUT_SECONDS", "10")
            ),
        )
    except (TypeError, ValueError, ValidationError):
        raise SmokeGuardError(SmokeGuardErrorCode.CREDENTIALS_UNAVAILABLE) from None
    if not settings.hubspot_configured:
        raise SmokeGuardError(SmokeGuardErrorCode.CREDENTIALS_UNAVAILABLE)
    return SmokeConfiguration(settings=settings, portal_id=portal_id)


def _synthetic_contact_email(run_id: UUID) -> str:
    return f"hubspot-smoke+{run_id}@example.com"


def _parse_synthetic_contact_email(email: str) -> UUID | None:
    match = _SMOKE_EMAIL_PATTERN.fullmatch(email)
    if match is None:
        return None
    try:
        run_id = UUID(match.group(1))
    except ValueError:
        return None
    return run_id if email == _synthetic_contact_email(run_id) else None


def _valid_provider_id(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


def _result(
    outcome: SmokeOutcome,
    cleanup: SmokeCleanup,
    *,
    error: SmokeErrorCode | None = None,
    contact_id: str | None = None,
) -> SmokeRunResult:
    return SmokeRunResult(
        outcome=outcome,
        cleanup=cleanup,
        error_code=error,
        contact_id=contact_id if _valid_provider_id(contact_id) else None,
    )


async def run_hubspot_smoke_operations(
    configuration: SmokeConfiguration,
    client: HubSpotClient,
    *,
    run_id: UUID | None = None,
) -> SmokeRunResult:
    """Run one exact-email Contact smoke after same-key account preflight."""
    email = _synthetic_contact_email(run_id or uuid4())
    try:
        preflight = await client.preflight_account()
    except HubSpotProviderError:
        return _result(
            SmokeOutcome.NOT_RUN,
            SmokeCleanup.NOT_NEEDED,
            error=SmokeErrorCode.PROVIDER_UNAVAILABLE,
        )
    if (
        preflight.outcome is not HubSpotPreflightOutcome.VERIFIED
        or not preflight.account_identity_verified
    ):
        return _result(
            SmokeOutcome.NOT_RUN,
            SmokeCleanup.NOT_NEEDED,
            error=SmokeErrorCode.ACCOUNT_MISMATCH,
        )
    try:
        client._activate_guarded_smoke_writes(
            portal_id=configuration.portal_id,
            capability_confirmation=HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION,
            contact_email=email,
        )
    except HubSpotProviderError:
        return _result(
            SmokeOutcome.NOT_RUN,
            SmokeCleanup.NOT_NEEDED,
            error=SmokeErrorCode.CAPABILITY_REVIEW_REQUIRED,
        )

    created_contact_id: str | None = None
    create_was_uncertain = False
    workflow_verified = False
    workflow_error: SmokeErrorCode | None = None
    try:
        try:
            await client.get_contact_by_email(email)
        except HubSpotProviderError as error:
            if error.code is not HubSpotErrorCode.NOT_FOUND:
                raise
        else:
            return _result(
                SmokeOutcome.FAILED,
                SmokeCleanup.NOT_NEEDED,
                error=SmokeErrorCode.CONTACT_ALREADY_EXISTS,
            )

        try:
            created = await client.create_contact(email=email)
        except HubSpotProviderError as error:
            if not error.outcome_uncertain:
                workflow_error = SmokeErrorCode.PROVIDER_UNAVAILABLE
            else:
                create_was_uncertain = True
                try:
                    created = await client.get_contact_by_email(email)
                except HubSpotProviderError as read_error:
                    if read_error.code is HubSpotErrorCode.NOT_FOUND:
                        return _result(
                            SmokeOutcome.UNCERTAIN,
                            SmokeCleanup.UNCERTAIN,
                            error=SmokeErrorCode.CLEANUP_UNCERTAIN,
                        )
                    workflow_error = SmokeErrorCode.PROVIDER_UNAVAILABLE
                else:
                    if created.email == email and not created.archived:
                        created_contact_id = created.id
                        create_was_uncertain = False
        else:
            if created.id == "" or created.email != email or created.archived:
                workflow_error = SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED
            else:
                created_contact_id = created.id

        if created_contact_id is not None:
            try:
                verified = await client.verify_smoke_contact(
                    created_contact_id, expected_email=email
                )
            except HubSpotProviderError:
                workflow_error = SmokeErrorCode.PROVIDER_UNAVAILABLE
            else:
                workflow_verified = (
                    verified.id == created_contact_id
                    and verified.email == email
                    and not verified.archived
                )
                if not workflow_verified:
                    workflow_error = SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED
        elif workflow_error is None:
            workflow_error = SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED
    except HubSpotProviderError as error:
        workflow_error = (
            SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED
            if error.code is HubSpotErrorCode.CONFLICT
            else SmokeErrorCode.PROVIDER_UNAVAILABLE
        )

    if created_contact_id is None:
        cleanup = SmokeCleanup.UNCERTAIN if create_was_uncertain else SmokeCleanup.NOT_NEEDED
        outcome = SmokeOutcome.UNCERTAIN if create_was_uncertain else SmokeOutcome.FAILED
        return _result(
            outcome,
            cleanup,
            error=workflow_error or SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED,
        )

    try:
        await client.archive_smoke_contact(created_contact_id, expected_email=email)
    except HubSpotProviderError as error:
        cleanup = SmokeCleanup.UNCERTAIN if error.outcome_uncertain else SmokeCleanup.FAILED
        return _result(
            SmokeOutcome.UNCERTAIN if cleanup is SmokeCleanup.UNCERTAIN else SmokeOutcome.FAILED,
            cleanup,
            error=(
                SmokeErrorCode.CLEANUP_UNCERTAIN
                if cleanup is SmokeCleanup.UNCERTAIN
                else SmokeErrorCode.CLEANUP_FAILED
            ),
            contact_id=created_contact_id,
        )

    if workflow_error is not None or not workflow_verified:
        return _result(
            SmokeOutcome.FAILED,
            SmokeCleanup.COMPLETE,
            error=workflow_error or SmokeErrorCode.CONTACT_IDENTITY_UNVERIFIED,
            contact_id=created_contact_id,
        )
    return _result(
        SmokeOutcome.PASSED,
        SmokeCleanup.COMPLETE,
        contact_id=created_contact_id,
    )


async def run_smoke(
    configuration: SmokeConfiguration,
    *,
    transport: AsyncBaseTransport | None = None,
) -> SmokeRunResult:
    """Verify smoke database reachability before constructing HubSpotClient."""
    try:
        resources = create_database_resources(configuration.settings)
    except Exception:
        return _result(
            SmokeOutcome.NOT_RUN,
            SmokeCleanup.NOT_NEEDED,
            error=SmokeErrorCode.DATABASE_UNAVAILABLE,
        )
    try:
        try:
            async with resources.engine.connect() as connection:
                identity = await connection.execute(
                    text("SELECT current_database(), current_user, inet_server_port()")
                )
                database_name, database_user, server_port = identity.one()
        except Exception:
            return _result(
                SmokeOutcome.NOT_RUN,
                SmokeCleanup.NOT_NEEDED,
                error=SmokeErrorCode.DATABASE_UNAVAILABLE,
            )
        if (
            database_name != _SMOKE_DATABASE
            or database_user != _SMOKE_DATABASE_USER
            or server_port != 5432
        ):
            return _result(
                SmokeOutcome.NOT_RUN,
                SmokeCleanup.NOT_NEEDED,
                error=SmokeErrorCode.DATABASE_UNAVAILABLE,
            )
        try:
            async with HubSpotClient(configuration.settings, transport=transport) as client:
                return await run_hubspot_smoke_operations(configuration, client)
        except HubSpotProviderError:
            return _result(
                SmokeOutcome.NOT_RUN,
                SmokeCleanup.NOT_NEEDED,
                error=SmokeErrorCode.PROVIDER_UNAVAILABLE,
            )
    finally:
        await dispose_database(resources)


def main() -> int:
    """Run the manually invoked smoke path with bounded, credential-safe output."""
    try:
        configuration = build_smoke_configuration(os.environ)
    except SmokeGuardError as error:
        if error.code is SmokeGuardErrorCode.CREDENTIALS_UNAVAILABLE:
            print("LIVE_SMOKE_NOT_RUN: required smoke credentials unavailable")
        else:
            print(f"HubSpot smoke refused: {error.code.value}")
        return 2
    result = asyncio.run(run_smoke(configuration))
    if result.outcome is SmokeOutcome.PASSED:
        print("HubSpot developer smoke passed; owned Contact cleanup completed")
        return 0
    print(
        "LIVE_SMOKE_NOT_RUN: "
        f"outcome={result.outcome.value}; cleanup={result.cleanup.value}; "
        f"error={result.error_code.value if result.error_code else 'none'}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

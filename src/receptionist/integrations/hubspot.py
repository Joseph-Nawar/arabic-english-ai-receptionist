"""Bounded HTTP access to HubSpot Contacts, Notes, and account identity.

Routes in this module follow HubSpot's documented CRM ``2026-09`` object and
search APIs. Account identity uses the documented Account Info ``2026-03``
route. This client does not persist application state or authorize CRM writes.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx
import phonenumbers

from receptionist.core.config import Settings

API_ORIGIN = "https://api.hubapi.com"
CONTACT_PROPERTIES = ("email", "phone", "mobilephone")
NOTE_PROPERTIES = ("hs_note_body", "hs_timestamp")
CONTACT_SEARCH_LIMIT = 20
NOTE_SEARCH_LIMIT = 5
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_NOTE_BODY_CHARS = 4000
MAX_RETRY_AFTER_SECONDS = 300
NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID = 202
HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION = (
    "I_VERIFIED_HUBSPOT_DEVELOPER_CONTACT_READ_WRITE_AND_ACCOUNT_PREFLIGHT"
)
_SMOKE_CONTACT_EMAIL_PATTERN = re.compile(
    r"^hubspot-smoke\+[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}@example\.com$"
)


class HubSpotErrorCode(StrEnum):
    """Finite, provider-safe failure categories returned by this boundary."""

    NOT_CONFIGURED = "not_configured"
    LIVE_TRANSPORT_DISABLED = "live_transport_disabled"
    ACTIVATION_REQUIRED = "activation_required"
    INVALID_REQUEST = "invalid_request"
    VALIDATION = "validation"
    UNAUTHORIZED = "unauthorized"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_RESPONSE = "invalid_response"


class HubSpotPreflightOutcome(StrEnum):
    """Safe outcome of the Service Key account-identity check."""

    VERIFIED = "verified"
    NOT_CONFIGURED = "not_configured"
    ACCOUNT_MISMATCH = "account_mismatch"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_RESPONSE = "invalid_response"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


class HubSpotCapabilityStatus(StrEnum):
    """Whether the key's required CRM permissions have been established."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class HubSpotPreflightResult:
    """Safe account-binding result, separate from CRM capability activation."""

    outcome: HubSpotPreflightOutcome
    account_identity_verified: bool
    capability_status: HubSpotCapabilityStatus

    @property
    def writes_enabled(self) -> bool:
        """Writes require verified identity and permissions; permissions remain unknown."""
        return (
            self.outcome is HubSpotPreflightOutcome.VERIFIED
            and self.account_identity_verified
            and self.capability_status is HubSpotCapabilityStatus.VERIFIED
        )


@dataclass(frozen=True, slots=True)
class HubSpotAccountSnapshot:
    """Minimum account detail needed to bind a Service Key to a portal."""

    portal_id: str


class HubSpotProviderError(Exception):
    """Safe HubSpot failure without provider body, request, or credential data."""

    def __init__(
        self,
        code: HubSpotErrorCode,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        outcome_uncertain: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.outcome_uncertain = outcome_uncertain
        super().__init__(f"HubSpot request failed: {code.value}")


@dataclass(frozen=True, slots=True)
class HubSpotContactSnapshot:
    id: str
    email: str | None
    phone: str | None
    mobilephone: str | None
    archived: bool


@dataclass(frozen=True, slots=True)
class HubSpotContactSearchResult:
    results: tuple[HubSpotContactSnapshot, ...]
    has_more: bool
    next_after: str | None

    @property
    def complete(self) -> bool:
        return not self.has_more


@dataclass(frozen=True, slots=True)
class HubSpotNoteSnapshot:
    id: str
    body: str | None
    timestamp: str | None
    contact_ids: tuple[str, ...]
    contact_associations_complete: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class HubSpotNoteSearchResult:
    results: tuple[HubSpotNoteSnapshot, ...]
    has_more: bool
    next_after: str | None

    @property
    def complete(self) -> bool:
        return not self.has_more


class HubSpotClient:
    """Concrete, single-account async HTTP boundary for Contacts and Notes."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.hubspot_configured or settings.hubspot_service_key is None:
            raise HubSpotProviderError(HubSpotErrorCode.NOT_CONFIGURED)
        if not _valid_portal_id(settings.hubspot_portal_id):
            raise HubSpotProviderError(HubSpotErrorCode.NOT_CONFIGURED)
        if settings.app_env in {"test", "ci"} and not isinstance(transport, httpx.MockTransport):
            raise HubSpotProviderError(HubSpotErrorCode.LIVE_TRANSPORT_DISABLED)

        self._settings = settings
        self._service_key = settings.hubspot_service_key
        self._expected_portal_id = settings.hubspot_portal_id
        self._timeout_seconds = settings.hubspot_request_timeout_seconds
        self._mock_transport = isinstance(transport, httpx.MockTransport)
        self._preflight_result: HubSpotPreflightResult | None = None
        self._smoke_contact_email: str | None = None
        self._smoke_contact_id: str | None = None
        self._smoke_writes_enabled = False
        self._smoke_contact_create_used = False
        self._client = httpx.AsyncClient(
            base_url=API_ORIGIN,
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> HubSpotClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_account_details(self) -> HubSpotAccountSnapshot:
        """Read the account portal ID via the documented 2026-03 Account Info route."""
        payload = await self._request_json("GET", "/account-info/2026-03/details")
        portal_id = payload.get("portalId")
        if not isinstance(portal_id, int) or isinstance(portal_id, bool) or portal_id <= 0:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
        return HubSpotAccountSnapshot(portal_id=str(portal_id))

    async def preflight_account(self) -> HubSpotPreflightResult:
        """Verify account identity on this client without activating write capability."""
        if not self._configuration_is_current():
            result = _preflight_result(HubSpotPreflightOutcome.VERIFICATION_UNAVAILABLE)
            self._preflight_result = result
            return result

        try:
            account = await self.get_account_details()
        except HubSpotProviderError as error:
            result = _preflight_error_result(error)
        else:
            if account.portal_id == self._expected_portal_id:
                result = _preflight_result(
                    HubSpotPreflightOutcome.VERIFIED,
                    account_identity_verified=True,
                )
            else:
                result = _preflight_result(HubSpotPreflightOutcome.ACCOUNT_MISMATCH)
        self._preflight_result = result
        return result

    async def get_contact_by_id(self, contact_id: str) -> HubSpotContactSnapshot:
        record_id = _record_id(contact_id)
        payload = await self._request_json(
            "GET",
            f"/crm/objects/2026-09/contacts/{record_id}",
            params={"properties": ",".join(CONTACT_PROPERTIES)},
        )
        return _parse_contact(payload)

    async def get_contact_by_email(self, email: str) -> HubSpotContactSnapshot:
        email_value = _nonblank_text(email, max_length=254)
        payload = await self._request_json(
            "GET",
            f"/crm/objects/2026-09/contacts/{quote(email_value, safe='')}",
            params={
                "idProperty": "email",
                "properties": ",".join(CONTACT_PROPERTIES),
            },
        )
        return _parse_contact(payload)

    async def search_contacts_by_email(
        self, email: str, *, after: str | None = None
    ) -> HubSpotContactSearchResult:
        email_value = _nonblank_text(email, max_length=254)
        return await self._search_contacts(
            [{"propertyName": "email", "operator": "EQ", "value": email_value}],
            after=after,
        )

    async def search_contacts_by_phone(
        self, phone: str, *, after: str | None = None
    ) -> HubSpotContactSearchResult:
        phone_value = _phone_search_value(phone)
        return await self._search_contacts(
            [
                {"propertyName": "phone", "operator": "EQ", "value": phone_value},
                {"propertyName": "mobilephone", "operator": "EQ", "value": phone_value},
            ],
            after=after,
        )

    async def _search_contacts(
        self, filters: list[dict[str, str]], *, after: str | None
    ) -> HubSpotContactSearchResult:
        next_page = _page_cursor(after)
        body: dict[str, Any] = {
            "filterGroups": [{"filters": [item]} for item in filters],
            "properties": list(CONTACT_PROPERTIES),
            "limit": CONTACT_SEARCH_LIMIT,
        }
        if next_page is not None:
            body["after"] = next_page
        payload = await self._request_json(
            "POST", "/crm/objects/2026-09/contacts/search", json_body=body
        )
        rows, has_more, cursor = _parse_search_page(payload)
        return HubSpotContactSearchResult(
            results=tuple(_parse_contact(row) for row in rows),
            has_more=has_more,
            next_after=cursor,
        )

    async def create_contact(
        self, *, email: str, phone: str | None = None
    ) -> HubSpotContactSnapshot:
        if self._smoke_writes_enabled:
            if (
                phone is not None
                or email != self._smoke_contact_email
                or self._smoke_contact_create_used
            ):
                raise HubSpotProviderError(HubSpotErrorCode.ACTIVATION_REQUIRED)
            self._assert_write_activation(smoke_contact_email=email)
            self._smoke_contact_create_used = True
        else:
            self._assert_write_activation()
        properties: dict[str, str] = {"email": _nonblank_text(email, max_length=254)}
        if phone is not None:
            properties["phone"] = _nonblank_text(phone, max_length=64)
        try:
            payload = await self._request_json(
                "POST",
                "/crm/objects/2026-09/contacts",
                json_body={"properties": properties},
                write=True,
            )
            return _parse_contact(payload)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.INVALID_RESPONSE:
                raise _uncertain(error) from None
            raise

    async def update_contact(
        self,
        contact_id: str,
        *,
        email: str | None = None,
        phone: str | None = None,
    ) -> HubSpotContactSnapshot:
        self._assert_write_activation()
        record_id = _record_id(contact_id)
        properties: dict[str, str] = {}
        if email is not None:
            properties["email"] = _nonblank_text(email, max_length=254)
        if phone is not None:
            properties["phone"] = _nonblank_text(phone, max_length=64)
        if not properties:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        try:
            payload = await self._request_json(
                "PATCH",
                f"/crm/objects/2026-09/contacts/{record_id}",
                json_body={"properties": properties},
                write=True,
            )
            return _parse_contact(payload)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.INVALID_RESPONSE:
                raise _uncertain(error) from None
            raise

    async def create_note(
        self,
        contact_id: str,
        *,
        body: str,
        timestamp: datetime,
    ) -> HubSpotNoteSnapshot:
        self._assert_write_activation()
        contact_record_id = _record_id(contact_id)
        note_body = _nonblank_text(body, max_length=MAX_NOTE_BODY_CHARS)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        timestamp_value = (
            timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        try:
            payload = await self._request_json(
                "POST",
                "/crm/objects/2026-09/notes",
                json_body={
                    "properties": {"hs_timestamp": timestamp_value, "hs_note_body": note_body},
                    "associations": [
                        {
                            "to": {"id": contact_record_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID,
                                }
                            ],
                        }
                    ],
                },
                write=True,
            )
            return _parse_note(payload)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.INVALID_RESPONSE:
                raise _uncertain(error) from None
            raise

    async def search_notes(
        self, marker: str, *, after: str | None = None
    ) -> HubSpotNoteSearchResult:
        marker_value = _note_marker(marker)
        next_page = _page_cursor(after)
        body: dict[str, Any] = {
            "query": marker_value,
            "properties": list(NOTE_PROPERTIES),
            "limit": NOTE_SEARCH_LIMIT,
        }
        if next_page is not None:
            body["after"] = next_page
        payload = await self._request_json(
            "POST", "/crm/objects/2026-09/notes/search", json_body=body
        )
        rows, has_more, cursor = _parse_search_page(payload)
        return HubSpotNoteSearchResult(
            results=tuple(_parse_note(row) for row in rows),
            has_more=has_more,
            next_after=cursor,
        )

    async def get_note(self, note_id: str) -> HubSpotNoteSnapshot:
        record_id = _record_id(note_id)
        payload = await self._request_json(
            "GET",
            f"/crm/objects/2026-09/notes/{record_id}",
            params={
                "properties": ",".join(NOTE_PROPERTIES),
                "associations": "contact",
            },
        )
        return _parse_note(payload)

    async def ensure_note_contact_association(
        self,
        note_id: str,
        contact_id: str,
        *,
        expected_body: str,
        expected_timestamp: datetime,
    ) -> HubSpotNoteSnapshot:
        """Repair only a verified absent Note-to-Contact association and read it back."""
        self._assert_write_activation()
        note_record_id = _record_id(note_id)
        contact_record_id = _record_id(contact_id)
        note_body = _nonblank_text(expected_body, max_length=MAX_NOTE_BODY_CHARS)
        if (
            not isinstance(expected_timestamp, datetime)
            or expected_timestamp.tzinfo is None
            or expected_timestamp.utcoffset() is None
        ):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        expected_utc = expected_timestamp.astimezone(UTC)
        current = await self.get_note(note_record_id)
        if current.id != note_record_id:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=True)
        if not _note_snapshot_matches(current, note_body, expected_utc):
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)
        if not current.contact_associations_complete:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=True)
        if current.contact_ids == (contact_record_id,):
            return current
        if current.contact_ids:
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)

        try:
            await self._request_empty(
                "PUT",
                "/crm/objects/2026-09/notes/"
                f"{note_record_id}/associations/contact/{contact_record_id}/"
                f"{NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID}",
                write=True,
            )
        except HubSpotProviderError as error:
            if error.outcome_uncertain:
                try:
                    current = await self.get_note(note_record_id)
                except HubSpotProviderError as read_error:
                    if read_error.code in {
                        HubSpotErrorCode.INVALID_RESPONSE,
                        HubSpotErrorCode.RESPONSE_TOO_LARGE,
                    }:
                        raise _uncertain(read_error) from None
                    raise _uncertain(error) from None
                if (
                    current.id == note_record_id
                    and _note_snapshot_matches(current, note_body, expected_utc)
                    and current.contact_associations_complete
                    and current.contact_ids == (contact_record_id,)
                ):
                    return current
                if current.contact_ids:
                    raise HubSpotProviderError(
                        HubSpotErrorCode.CONFLICT,
                        outcome_uncertain=True,
                    ) from None
                if not current.contact_associations_complete:
                    raise _uncertain(error) from None
            if error.code in {
                HubSpotErrorCode.INVALID_RESPONSE,
                HubSpotErrorCode.RESPONSE_TOO_LARGE,
            }:
                raise _uncertain(error) from None
            raise

        try:
            repaired = await self.get_note(note_record_id)
        except HubSpotProviderError as error:
            raise _uncertain(error) from None
        if (
            repaired.id != note_record_id
            or not _note_snapshot_matches(repaired, note_body, expected_utc)
            or not repaired.contact_associations_complete
            or repaired.contact_ids != (contact_record_id,)
        ):
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT, outcome_uncertain=True)
        return repaired

    async def verify_smoke_contact(
        self, contact_id: str, *, expected_email: str
    ) -> HubSpotContactSnapshot:
        """Bind cleanup to a fresh read of this run's exact synthetic Contact."""
        self._assert_write_activation(smoke_contact_email=expected_email)
        record_id = _record_id(contact_id)
        if not _is_smoke_contact_email(expected_email):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        current = await self.get_contact_by_id(record_id)
        if current.id != record_id or current.email != expected_email or current.archived:
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)
        self._smoke_contact_id = record_id
        return current

    async def archive_smoke_contact(self, contact_id: str, *, expected_email: str) -> None:
        """Archive only the active synthetic Contact created by the current smoke run."""
        self._assert_write_activation(smoke_contact_email=expected_email)
        record_id = _record_id(contact_id)
        if not _is_smoke_contact_email(expected_email) or self._smoke_contact_id != record_id:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        current = await self.get_contact_by_id(record_id)
        if current.id != record_id or current.email != expected_email:
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)
        if current.archived:
            return
        await self._request_empty(
            "DELETE", f"/crm/objects/2026-09/contacts/{record_id}", write=True
        )

    def _activate_guarded_smoke_writes(
        self,
        *,
        portal_id: str,
        capability_confirmation: str,
        contact_email: str,
    ) -> None:
        """Enable one reviewed local smoke Contact after same-key identity preflight."""
        preflight = self._preflight_result
        if (
            self._settings.app_env != "local"
            or not self._configuration_is_current()
            or portal_id != self._expected_portal_id
            or capability_confirmation != HUBSPOT_SMOKE_CAPABILITY_CONFIRMATION
            or preflight is None
            or preflight.outcome is not HubSpotPreflightOutcome.VERIFIED
            or not preflight.account_identity_verified
            or not _is_smoke_contact_email(contact_email)
        ):
            raise HubSpotProviderError(HubSpotErrorCode.ACTIVATION_REQUIRED)
        self._smoke_contact_email = contact_email
        self._smoke_writes_enabled = True

    def _configuration_is_current(self) -> bool:
        """Reject settings mutation so preflight evidence stays bound to its config."""
        return (
            self._settings.hubspot_service_key is self._service_key
            and self._settings.hubspot_portal_id == self._expected_portal_id
            and self._settings.hubspot_configured
            and _valid_portal_id(self._settings.hubspot_portal_id)
        )

    def _assert_write_activation(self, *, smoke_contact_email: str | None = None) -> None:
        """Allow only isolated preflight-free test mocks; production stays fail-closed."""
        if self._smoke_writes_enabled:
            if (
                self._settings.app_env == "local"
                and self._configuration_is_current()
                and smoke_contact_email == self._smoke_contact_email
            ):
                return
            raise HubSpotProviderError(HubSpotErrorCode.ACTIVATION_REQUIRED)
        isolated_test_transport = self._settings.app_env in {"test", "ci"} and self._mock_transport
        if (
            isolated_test_transport
            and self._preflight_result is None
            and self._configuration_is_current()
        ):
            return
        if (
            self._preflight_result is not None
            and self._preflight_result.writes_enabled
            and self._configuration_is_current()
        ):
            return
        raise HubSpotProviderError(HubSpotErrorCode.ACTIVATION_REQUIRED)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        response_body = await self._request_bytes(
            method, path, params=params, json_body=json_body, write=write
        )
        try:
            parsed = json.loads(response_body)
        except (UnicodeDecodeError, ValueError):
            raise HubSpotProviderError(
                HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=write
            ) from None
        if not isinstance(parsed, dict):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=write)
        return parsed

    async def _request_empty(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
    ) -> None:
        body = await self._request_bytes(method, path, write=write, expect_json=False)
        if body:
            try:
                parsed = json.loads(body)
            except (UnicodeDecodeError, ValueError):
                raise HubSpotProviderError(
                    HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=write
                ) from None
            if not isinstance(parsed, dict):
                raise HubSpotProviderError(
                    HubSpotErrorCode.INVALID_RESPONSE, outcome_uncertain=write
                )

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
        expect_json: bool = True,
    ) -> bytes:
        request = self._client.build_request(
            method,
            path,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {self._service_key.get_secret_value()}"},
        )
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.send(request, stream=True)
                if not 200 <= response.status_code < 300:
                    raise _status_error(response, write=write)
                declared_length = response.headers.get("content-length")
                if (
                    declared_length is not None
                    and declared_length.isdecimal()
                    and int(declared_length) > MAX_RESPONSE_BYTES
                ):
                    raise HubSpotProviderError(
                        HubSpotErrorCode.RESPONSE_TOO_LARGE,
                        outcome_uncertain=write,
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise HubSpotProviderError(
                            HubSpotErrorCode.RESPONSE_TOO_LARGE,
                            outcome_uncertain=write,
                        )
                    chunks.extend(chunk)
                if expect_json and not chunks:
                    raise HubSpotProviderError(
                        HubSpotErrorCode.INVALID_RESPONSE,
                        outcome_uncertain=write,
                    )
                return bytes(chunks)
        except HubSpotProviderError:
            raise
        except httpx.ConnectTimeout:
            raise HubSpotProviderError(
                HubSpotErrorCode.TIMEOUT,
                outcome_uncertain=write and response is not None,
            ) from None
        except (httpx.TimeoutException, TimeoutError):
            raise HubSpotProviderError(
                HubSpotErrorCode.TIMEOUT,
                outcome_uncertain=write,
            ) from None
        except httpx.ConnectError:
            raise HubSpotProviderError(
                HubSpotErrorCode.PROVIDER_UNAVAILABLE,
                outcome_uncertain=write and response is not None,
            ) from None
        except httpx.RequestError:
            raise HubSpotProviderError(
                HubSpotErrorCode.PROVIDER_UNAVAILABLE,
                outcome_uncertain=write,
            ) from None
        finally:
            if response is not None:
                with suppress(httpx.RequestError):
                    await response.aclose()


def _status_error(response: httpx.Response, *, write: bool) -> HubSpotProviderError:
    status = response.status_code
    if status == 400:
        code = HubSpotErrorCode.VALIDATION
    elif status == 401:
        code = HubSpotErrorCode.UNAUTHORIZED
    elif status == 403:
        code = HubSpotErrorCode.PERMISSION_DENIED
    elif status == 404:
        code = HubSpotErrorCode.NOT_FOUND
    elif status == 409:
        code = HubSpotErrorCode.CONFLICT
    elif status == 429:
        code = HubSpotErrorCode.RATE_LIMITED
    elif status >= 500:
        code = HubSpotErrorCode.PROVIDER_UNAVAILABLE
    else:
        code = HubSpotErrorCode.PROVIDER_UNAVAILABLE
    retry_after = _retry_after(response.headers.get("retry-after"))
    return HubSpotProviderError(
        code,
        status_code=status,
        retry_after_seconds=retry_after,
        outcome_uncertain=write and status >= 500,
    )


def _uncertain(error: HubSpotProviderError) -> HubSpotProviderError:
    """Mark an unusable post-write response without exposing its source details."""
    if error.outcome_uncertain:
        return error
    return HubSpotProviderError(
        error.code,
        status_code=error.status_code,
        retry_after_seconds=error.retry_after_seconds,
        outcome_uncertain=True,
    )


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    if value.isascii() and value.isdecimal():
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        return None
    remaining = max(0, int((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()))
    return min(remaining, MAX_RETRY_AFTER_SECONDS)


def _record_id(value: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    try:
        if int(value) <= 0:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    except ValueError:
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST) from None
    return value


def _page_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 20
    ):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    return value


def _nonblank_text(value: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    return normalized


def _note_marker(value: str) -> str:
    marker = _nonblank_text(value, max_length=100)
    prefix = "booking-execution:"
    if not marker.startswith(prefix):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    try:
        execution_id = uuid.UUID(marker[len(prefix) :])
    except (ValueError, AttributeError):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST) from None
    if marker != f"{prefix}{execution_id}":
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    return marker


def _phone_search_value(value: str) -> str:
    """Convert canonical E.164 to the national number HubSpot searches by."""
    phone_value = _nonblank_text(value, max_length=64)
    try:
        parsed = phonenumbers.parse(phone_value, None)
    except phonenumbers.NumberParseException:
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST) from None
    if (
        not phonenumbers.is_valid_number(parsed)
        or phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164) != phone_value
    ):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
    return phonenumbers.national_significant_number(parsed)


def _parse_search_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str | None]:
    rows = payload.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    total = payload.get("total")
    if total is not None and (not isinstance(total, int) or isinstance(total, bool) or total < 0):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    cursor: str | None = None
    paging = payload.get("paging")
    if paging is not None:
        if not isinstance(paging, dict):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
        next_page = paging.get("next")
        if next_page is not None:
            if not isinstance(next_page, dict):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
            after = next_page.get("after")
            if not isinstance(after, (str, int)) or isinstance(after, bool):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
            cursor = str(after)
            if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 20:
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    has_more = cursor is not None or (total is not None and total > len(rows))
    return rows, has_more, cursor


def _parse_contact(payload: dict[str, Any]) -> HubSpotContactSnapshot:
    record_id = payload.get("id")
    properties = payload.get("properties")
    if not isinstance(record_id, str) or not isinstance(properties, dict):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
    try:
        record_id = _record_id(record_id)
    except HubSpotProviderError:
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE) from None
    archived = payload.get("archived", False)
    if not isinstance(archived, bool):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
    return HubSpotContactSnapshot(
        id=record_id,
        email=_optional_string(properties, "email"),
        phone=_optional_string(properties, "phone"),
        mobilephone=_optional_string(properties, "mobilephone"),
        archived=archived,
    )


def _parse_note(payload: dict[str, Any]) -> HubSpotNoteSnapshot:
    record_id = payload.get("id")
    properties = payload.get("properties")
    if not isinstance(record_id, str) or not isinstance(properties, dict):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
    try:
        record_id = _record_id(record_id)
    except HubSpotProviderError:
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE) from None
    archived = payload.get("archived")
    if not isinstance(archived, bool):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    contact_ids: tuple[str, ...] = ()
    contact_associations_complete = False
    associations = payload.get("associations")
    if associations is not None:
        if not isinstance(associations, dict):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
        contacts = associations.get("contacts")
        if contacts is not None:
            if not isinstance(contacts, dict):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
            if "results" in contacts:
                results = contacts["results"]
                if not isinstance(results, list) or any(
                    not isinstance(item, dict) for item in results
                ):
                    raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
                try:
                    contact_ids = tuple(
                        _record_id(_required_string(item, "id")) for item in results
                    )
                except HubSpotProviderError:
                    raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE) from None
                paging = contacts.get("paging")
                if "paging" not in contacts or paging is None:
                    contact_associations_complete = True
                elif isinstance(paging, dict):
                    next_page = paging.get("next")
                    if next_page is None:
                        contact_associations_complete = True
                    elif (
                        isinstance(next_page, dict)
                        and isinstance(next_page.get("after"), str)
                        and next_page["after"]
                    ):
                        contact_associations_complete = False
                    else:
                        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
                else:
                    raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    return HubSpotNoteSnapshot(
        id=record_id,
        body=_optional_string(properties, "hs_note_body"),
        timestamp=_optional_string(properties, "hs_timestamp"),
        contact_ids=contact_ids,
        contact_associations_complete=contact_associations_complete,
        archived=archived,
    )


def _note_snapshot_matches(
    note: HubSpotNoteSnapshot,
    expected_body: str,
    expected_timestamp_utc: datetime,
) -> bool:
    if note.archived or note.body != expected_body or note.timestamp is None:
        return False
    try:
        timestamp = datetime.fromisoformat(note.timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        timestamp.tzinfo is not None
        and timestamp.utcoffset() is not None
        and timestamp.astimezone(UTC) == expected_timestamp_utc
    )


def _optional_string(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
    return value


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
    return value


async def preflight_hubspot(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HubSpotPreflightResult:
    """Return a safe binding result, including a no-request result if unconfigured."""
    if not _settings_are_configured(settings):
        return _preflight_result(HubSpotPreflightOutcome.NOT_CONFIGURED)
    try:
        async with HubSpotClient(settings, transport=transport) as client:
            return await client.preflight_account()
    except HubSpotProviderError as error:
        return _preflight_error_result(error)


def _settings_are_configured(settings: Settings) -> bool:
    key = settings.hubspot_service_key
    return (
        key is not None
        and bool(key.get_secret_value().strip())
        and _valid_portal_id(settings.hubspot_portal_id)
    )


def _valid_portal_id(value: str | None) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


def _is_smoke_contact_email(value: str) -> bool:
    return _SMOKE_CONTACT_EMAIL_PATTERN.fullmatch(value) is not None


def _preflight_result(
    outcome: HubSpotPreflightOutcome,
    *,
    account_identity_verified: bool = False,
) -> HubSpotPreflightResult:
    return HubSpotPreflightResult(
        outcome=outcome,
        account_identity_verified=account_identity_verified,
        capability_status=HubSpotCapabilityStatus.UNVERIFIED,
    )


def _preflight_error_result(error: HubSpotProviderError) -> HubSpotPreflightResult:
    if error.code in {
        HubSpotErrorCode.UNAUTHORIZED,
        HubSpotErrorCode.PERMISSION_DENIED,
    }:
        outcome = HubSpotPreflightOutcome.PERMISSION_DENIED
    elif error.code is HubSpotErrorCode.NOT_CONFIGURED:
        outcome = HubSpotPreflightOutcome.NOT_CONFIGURED
    elif error.code in {
        HubSpotErrorCode.TIMEOUT,
        HubSpotErrorCode.RATE_LIMITED,
        HubSpotErrorCode.PROVIDER_UNAVAILABLE,
    }:
        outcome = HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE
    elif error.code in {
        HubSpotErrorCode.INVALID_RESPONSE,
        HubSpotErrorCode.RESPONSE_TOO_LARGE,
    }:
        outcome = HubSpotPreflightOutcome.INVALID_RESPONSE
    else:
        outcome = HubSpotPreflightOutcome.VERIFICATION_UNAVAILABLE
    return _preflight_result(outcome)

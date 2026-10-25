"""Bounded HTTP access to HubSpot Contacts and Notes.

Routes in this module follow HubSpot's documented CRM ``2026-09`` object and
search APIs. Account identity preflight and application operations belong to
later, separate layers; this client does not persist application state.
"""

from __future__ import annotations

import asyncio
import json
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


class HubSpotErrorCode(StrEnum):
    """Finite, provider-safe failure categories returned by this boundary."""

    NOT_CONFIGURED = "not_configured"
    LIVE_TRANSPORT_DISABLED = "live_transport_disabled"
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
        if settings.app_env in {"test", "ci"} and not isinstance(transport, httpx.MockTransport):
            raise HubSpotProviderError(HubSpotErrorCode.LIVE_TRANSPORT_DISABLED)

        self._service_key = settings.hubspot_service_key
        self._timeout_seconds = settings.hubspot_request_timeout_seconds
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
        properties: dict[str, str] = {"email": _nonblank_text(email, max_length=254)}
        if phone is not None:
            properties["phone"] = _nonblank_text(phone, max_length=64)
        payload = await self._request_json(
            "POST",
            "/crm/objects/2026-09/contacts",
            json_body={"properties": properties},
            write=True,
        )
        return _parse_contact(payload)

    async def update_contact(
        self,
        contact_id: str,
        *,
        email: str | None = None,
        phone: str | None = None,
    ) -> HubSpotContactSnapshot:
        record_id = _record_id(contact_id)
        properties: dict[str, str] = {}
        if email is not None:
            properties["email"] = _nonblank_text(email, max_length=254)
        if phone is not None:
            properties["phone"] = _nonblank_text(phone, max_length=64)
        if not properties:
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        payload = await self._request_json(
            "PATCH",
            f"/crm/objects/2026-09/contacts/{record_id}",
            json_body={"properties": properties},
            write=True,
        )
        return _parse_contact(payload)

    async def create_note(
        self,
        contact_id: str,
        *,
        body: str,
        timestamp: datetime,
    ) -> HubSpotNoteSnapshot:
        contact_record_id = _record_id(contact_id)
        note_body = _nonblank_text(body, max_length=MAX_NOTE_BODY_CHARS)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
        timestamp_value = (
            timestamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
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
        self, note_id: str, contact_id: str
    ) -> HubSpotNoteSnapshot:
        """Repair only a verified absent Note-to-Contact association and read it back."""
        note_record_id = _record_id(note_id)
        contact_record_id = _record_id(contact_id)
        current = await self.get_note(note_record_id)
        if current.contact_ids == (contact_record_id,):
            return current
        if current.contact_ids:
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)

        await self._request_empty(
            "PUT",
            "/crm/objects/2026-09/notes/"
            f"{note_record_id}/associations/contact/{contact_record_id}/"
            f"{NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID}",
            write=True,
        )
        repaired = await self.get_note(note_record_id)
        if repaired.contact_ids != (contact_record_id,):
            raise HubSpotProviderError(HubSpotErrorCode.CONFLICT)
        return repaired

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
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE) from None
        if not isinstance(parsed, dict):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
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
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE) from None
            if not isinstance(parsed, dict):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

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
                    raise HubSpotProviderError(HubSpotErrorCode.RESPONSE_TOO_LARGE)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise HubSpotProviderError(HubSpotErrorCode.RESPONSE_TOO_LARGE)
                    chunks.extend(chunk)
                if expect_json and not chunks:
                    raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
                return bytes(chunks)
        except HubSpotProviderError:
            raise
        except httpx.ConnectTimeout:
            raise HubSpotProviderError(HubSpotErrorCode.TIMEOUT) from None
        except (httpx.TimeoutException, TimeoutError):
            raise HubSpotProviderError(
                HubSpotErrorCode.TIMEOUT,
                outcome_uncertain=write,
            ) from None
        except httpx.ConnectError:
            raise HubSpotProviderError(HubSpotErrorCode.PROVIDER_UNAVAILABLE) from None
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
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or int(value) <= 0
    ):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_REQUEST)
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
    if not marker.startswith("source-op:"):
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
    record_id = _record_id(record_id)
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
    record_id = _record_id(record_id)
    archived = payload.get("archived", False)
    if not isinstance(archived, bool):
        raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)

    contact_ids: tuple[str, ...] = ()
    associations = payload.get("associations")
    if associations is not None:
        if not isinstance(associations, dict):
            raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
        contacts = associations.get("contacts")
        if contacts is not None:
            if not isinstance(contacts, dict):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
            results = contacts.get("results", [])
            if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                raise HubSpotProviderError(HubSpotErrorCode.INVALID_RESPONSE)
            contact_ids = tuple(_record_id(_required_string(item, "id")) for item in results)

    return HubSpotNoteSnapshot(
        id=record_id,
        body=_optional_string(properties, "hs_note_body"),
        timestamp=_optional_string(properties, "hs_timestamp"),
        contact_ids=contact_ids,
        archived=archived,
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

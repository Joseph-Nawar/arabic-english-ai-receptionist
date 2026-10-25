"""Durable, fail-closed resolution of local Contacts to HubSpot Contacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.application.booking import ToolExecutionClaimOutcome, _claim_tool_execution
from receptionist.application.crm_identity import (
    CRMContactIdentityDecision,
    CRMContactIdentityOutcome,
    decide_crm_contact_identity,
)
from receptionist.core.config import Settings
from receptionist.db.models import (
    AuditEvent,
    BusinessConfig,
    Contact,
    Conversation,
    CRMContactLink,
    CRMContactLinkState,
    ToolExecution,
)
from receptionist.domain.enums import AuditActorType, ToolExecutionStatus
from receptionist.domain.identity import (
    CRMContactLinkProvenance,
    PhoneNormalizationError,
    normalize_phone_number,
)
from receptionist.integrations.hubspot import (
    HubSpotCapabilityStatus,
    HubSpotClient,
    HubSpotContactSnapshot,
    HubSpotErrorCode,
    HubSpotPreflightOutcome,
    HubSpotPreflightResult,
    HubSpotProviderError,
)

_OPERATION: Literal["resolve_crm_contact"] = "resolve_crm_contact"
_EMAIL_LIMIT = 254
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


class CRMContactResolutionOutcome(StrEnum):
    LINKED = "linked"
    FOUND_AND_LINKED = "found_and_linked"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    STALE_MAPPING = "stale_mapping"
    IDENTITY_CONFLICT = "identity_conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_CONFIGURED = "not_configured"
    INVALID_REQUEST = "invalid_request"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_IN_PROGRESS = "operation_in_progress"


class CRMResolutionErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    BUSINESS_CONFIGURATION_MISSING = "business_configuration_missing"
    NOT_CONFIGURED = "not_configured"
    ACCOUNT_MISMATCH = "account_mismatch"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    IDENTITY_CONFLICT = "identity_conflict"
    LOCAL_STATE_CHANGED = "local_state_changed"
    LOCAL_PERSISTENCE_FAILURE = "local_persistence_failure"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_IN_PROGRESS = "operation_in_progress"


class CRMResolutionNextAction(StrEnum):
    NONE = "none"
    RETRY_SAME_KEY = "retry_same_key"
    RECONCILE_SAME_KEY = "reconcile_same_key"
    OPERATOR_REVIEW = "operator_review"


class _CRMModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CRMContactResolutionRequest(_CRMModel):
    conversation_id: UUID
    contact_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=_IDEMPOTENCY_PATTERN)
    trusted_verified_email: str | None = Field(default=None, max_length=_EMAIL_LIMIT)

    @field_validator("trusted_verified_email")
    @classmethod
    def validate_trusted_verified_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > _EMAIL_LIMIT
            or normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
            or any(character.isspace() or ord(character) < 32 for character in normalized)
        ):
            raise ValueError("trusted verified email must be a bounded email value")
        return normalized


class CRMContactResolutionResult(_CRMModel):
    operation: Literal["resolve_crm_contact"] = _OPERATION
    outcome: CRMContactResolutionOutcome
    hubspot_contact_id: str | None = Field(default=None, min_length=1, max_length=255)
    replayed: bool = False
    error_code: CRMResolutionErrorCode | None = None
    next_action: CRMResolutionNextAction = CRMResolutionNextAction.NONE


@dataclass(frozen=True, slots=True)
class _LinkSnapshot:
    portal_id: str
    hubspot_contact_id: str
    linked_by: CRMContactLinkProvenance
    state: CRMContactLinkState


@dataclass(frozen=True, slots=True)
class _ResolutionSnapshot:
    contact_id: UUID
    conversation_id: UUID
    phone_e164: str | None
    local_email: str | None
    default_phone_region: str | None
    link: _LinkSnapshot | None
    execution_id: UUID
    replayed: bool
    configuration_missing: bool


class _TerminalizationError(Exception):
    """The durable operation claim could not be safely completed."""


def _result(
    outcome: CRMContactResolutionOutcome,
    *,
    provider_id: str | None = None,
    replayed: bool = False,
    error_code: CRMResolutionErrorCode | None = None,
    next_action: CRMResolutionNextAction = CRMResolutionNextAction.NONE,
) -> CRMContactResolutionResult:
    return CRMContactResolutionResult(
        outcome=outcome,
        hubspot_contact_id=provider_id,
        replayed=replayed,
        error_code=error_code,
        next_action=next_action,
    )


async def resolve_crm_contact(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactResolutionRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CRMContactResolutionResult:
    """Resolve one owned local Contact through a same-key account-preflighted client."""
    try:
        loaded = await _load_local_state(session_factory, request)
    except SQLAlchemyError:
        return _result(
            CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
            error_code=CRMResolutionErrorCode.LOCAL_PERSISTENCE_FAILURE,
            next_action=CRMResolutionNextAction.RECONCILE_SAME_KEY,
        )
    if isinstance(loaded, CRMContactResolutionResult):
        return loaded
    snapshot = loaded

    if snapshot.configuration_missing:
        return await _finish(
            session_factory,
            snapshot,
            _result(
                CRMContactResolutionOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMResolutionErrorCode.BUSINESS_CONFIGURATION_MISSING,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            ),
        )

    trusted_email, evidence_error = _effective_verified_email(snapshot, request)
    if evidence_error is not None:
        return await _finish(
            session_factory,
            snapshot,
            evidence_error.model_copy(update={"replayed": snapshot.replayed}),
        )

    if snapshot.link is not None:
        if not _settings_configured(settings):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactResolutionOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMResolutionErrorCode.NOT_CONFIGURED,
                    next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
                ),
            )
        if snapshot.link.portal_id != settings.hubspot_portal_id:
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactResolutionOutcome.IDENTITY_CONFLICT,
                    replayed=snapshot.replayed,
                    error_code=CRMResolutionErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
                ),
            )
    elif snapshot.phone_e164 is None and trusted_email is None:
        return await _finish(
            session_factory,
            snapshot,
            _result(
                CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
                replayed=snapshot.replayed,
            ),
        )

    if not _settings_configured(settings):
        return await _finish(
            session_factory,
            snapshot,
            _result(
                CRMContactResolutionOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMResolutionErrorCode.NOT_CONFIGURED,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            ),
        )

    expected_portal_id = settings.hubspot_portal_id
    service_key = settings.hubspot_service_key
    try:
        client = HubSpotClient(settings, transport=transport)
    except HubSpotProviderError as error:
        return await _finish(
            session_factory,
            snapshot,
            _provider_error_result(error, replayed=snapshot.replayed),
        )

    async with client:
        preflight = await client.preflight_account()
        preflight_failure = _preflight_failure(preflight, replayed=snapshot.replayed)
        if preflight_failure is not None:
            return await _finish(session_factory, snapshot, preflight_failure)

        if snapshot.link is not None and snapshot.link.state is CRMContactLinkState.ACTIVE:
            active_link = snapshot.link
            try:
                provider_snapshot = await client.get_contact_by_id(active_link.hubspot_contact_id)
            except HubSpotProviderError as error:
                if error.code is not HubSpotErrorCode.NOT_FOUND:
                    return await _finish(
                        session_factory,
                        snapshot,
                        _provider_error_result(error, replayed=snapshot.replayed),
                    )
                provider_snapshot = None

            if provider_snapshot is None or provider_snapshot.archived:
                stale_result = await _mark_link_stale(session_factory, snapshot)
                if stale_result is not None:
                    return await _finish(session_factory, snapshot, stale_result)
                snapshot = replace(
                    snapshot,
                    link=replace(active_link, state=CRMContactLinkState.STALE),
                )
            else:
                if provider_snapshot.id != active_link.hubspot_contact_id:
                    return await _finish(
                        session_factory,
                        snapshot,
                        _result(
                            CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
                            replayed=snapshot.replayed,
                            error_code=CRMResolutionErrorCode.INVALID_PROVIDER_RESPONSE,
                            next_action=CRMResolutionNextAction.RETRY_SAME_KEY,
                        ),
                    )
                try:
                    verified_result = await _verify_active_link(
                        client,
                        snapshot,
                        provider_snapshot,
                        trusted_email=trusted_email,
                        replayed=snapshot.replayed,
                    )
                except HubSpotProviderError as error:
                    return await _finish(
                        session_factory,
                        snapshot,
                        _provider_error_result(error, replayed=snapshot.replayed),
                    )
                if verified_result is not None:
                    return await _finish(session_factory, snapshot, verified_result)
                if not _settings_unchanged(settings, service_key, expected_portal_id):
                    return await _finish(
                        session_factory,
                        snapshot,
                        _result(
                            CRMContactResolutionOutcome.NOT_CONFIGURED,
                            replayed=snapshot.replayed,
                            error_code=CRMResolutionErrorCode.ACCOUNT_MISMATCH,
                            next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
                        ),
                    )
                return await _apply_verified_link(
                    session_factory,
                    snapshot,
                    request,
                    settings,
                    expected_portal_id,
                    service_key,
                    provider_id=active_link.hubspot_contact_id,
                    provenance=active_link.linked_by,
                    trusted_email=trusted_email,
                    relinking=False,
                )
        elif snapshot.link is not None:
            pass  # A previously verified stale link is searched, never fetched by old ID.

        if not _settings_unchanged(settings, service_key, expected_portal_id):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactResolutionOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMResolutionErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
                ),
            )

        try:
            identity = await _resolve_unmapped_identity(
                client,
                phone_e164=snapshot.phone_e164,
                trusted_email=trusted_email,
                default_region=snapshot.default_phone_region or "",
            )
        except HubSpotProviderError as error:
            return await _finish(
                session_factory,
                snapshot,
                _provider_error_result(error, replayed=snapshot.replayed),
            )

    if identity.outcome is CRMContactIdentityOutcome.MATCHED:
        if identity.hubspot_contact_id is None or identity.linked_by is None:
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
                    replayed=snapshot.replayed,
                    error_code=CRMResolutionErrorCode.INVALID_PROVIDER_RESPONSE,
                    next_action=CRMResolutionNextAction.RETRY_SAME_KEY,
                ),
            )
        if not _settings_unchanged(settings, service_key, expected_portal_id):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactResolutionOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMResolutionErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
                ),
            )
        return await _apply_verified_link(
            session_factory,
            snapshot,
            request,
            settings,
            expected_portal_id,
            service_key,
            provider_id=identity.hubspot_contact_id,
            provenance=identity.linked_by,
            trusted_email=trusted_email,
            relinking=snapshot.link is not None,
        )

    outcome = _decision_outcome(identity.outcome)
    if outcome is CRMContactResolutionOutcome.NOT_FOUND and snapshot.link is not None:
        outcome = CRMContactResolutionOutcome.STALE_MAPPING
    return await _finish(
        session_factory,
        snapshot,
        _result(
            outcome,
            replayed=snapshot.replayed,
            error_code=(
                CRMResolutionErrorCode.IDENTITY_CONFLICT
                if outcome is CRMContactResolutionOutcome.IDENTITY_CONFLICT
                else None
            ),
            next_action=(
                CRMResolutionNextAction.OPERATOR_REVIEW
                if outcome
                in {
                    CRMContactResolutionOutcome.AMBIGUOUS,
                    CRMContactResolutionOutcome.IDENTITY_CONFLICT,
                    CRMContactResolutionOutcome.STALE_MAPPING,
                }
                else CRMResolutionNextAction.NONE
            ),
        ),
    )


async def _load_local_state(
    session_factory: async_sessionmaker[AsyncSession], request: CRMContactResolutionRequest
) -> _ResolutionSnapshot | CRMContactResolutionResult:
    async with session_factory.begin() as session:
        conversation = await session.get(Conversation, request.conversation_id)
        contact = await session.get(Contact, request.contact_id)
        if conversation is None or contact is None or conversation.contact_id != request.contact_id:
            return _result(
                CRMContactResolutionOutcome.INVALID_REQUEST,
                error_code=CRMResolutionErrorCode.INVALID_REQUEST,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )

        business = await session.get(BusinessConfig, 1)
        arguments: dict[str, object] = {
            "conversation_id": str(request.conversation_id),
            "contact_id": str(request.contact_id),
            "trusted_email_supplied": request.trusted_verified_email is not None,
        }
        claim = await _claim_tool_execution(
            session,
            conversation_id=request.conversation_id,
            tool_name=_OPERATION,
            idempotency_key=request.idempotency_key,
            sanitized_arguments=arguments,
        )
        if claim.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT:
            return _result(
                CRMContactResolutionOutcome.IDEMPOTENCY_CONFLICT,
                error_code=CRMResolutionErrorCode.IDEMPOTENCY_CONFLICT,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )
        if claim.outcome is ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS:
            return _result(
                CRMContactResolutionOutcome.OPERATION_IN_PROGRESS,
                error_code=CRMResolutionErrorCode.OPERATION_IN_PROGRESS,
                next_action=CRMResolutionNextAction.RETRY_SAME_KEY,
            )

        link = await session.get(CRMContactLink, request.contact_id)
        link_snapshot = (
            None
            if link is None
            else _LinkSnapshot(
                portal_id=link.hubspot_portal_id,
                hubspot_contact_id=link.hubspot_contact_id,
                linked_by=link.linked_by,
                state=link.state,
            )
        )
        return _ResolutionSnapshot(
            contact_id=contact.id,
            conversation_id=conversation.id,
            phone_e164=contact.phone_e164,
            local_email=contact.email,
            default_phone_region=None if business is None else business.default_phone_region,
            link=link_snapshot,
            execution_id=claim.execution.id,
            replayed=claim.outcome is not ToolExecutionClaimOutcome.NEW,
            configuration_missing=business is None,
        )


def _effective_verified_email(
    snapshot: _ResolutionSnapshot,
    request: CRMContactResolutionRequest,
) -> tuple[str | None, CRMContactResolutionResult | None]:
    link = snapshot.link
    if link is not None and link.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL:
        persisted = _normalized_email(snapshot.local_email)
        if persisted is None or persisted is _INVALID_EMAIL:
            return None, _result(
                CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
                error_code=CRMResolutionErrorCode.LOCAL_STATE_CHANGED,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )
        supplied = request.trusted_verified_email
        if supplied is not None and _normalized_email(supplied) != persisted:
            return None, _identity_conflict(snapshot.replayed)
        return snapshot.local_email, None

    supplied = request.trusted_verified_email
    if supplied is None:
        return None, None
    if snapshot.local_email is not None and _normalized_email(
        snapshot.local_email
    ) != _normalized_email(supplied):
        return None, _identity_conflict(snapshot.replayed)
    return supplied, None


async def _resolve_unmapped_identity(
    client: HubSpotClient,
    *,
    phone_e164: str | None,
    trusted_email: str | None,
    default_region: str,
) -> CRMContactIdentityDecision:
    phone_search = (
        await client.search_contacts_by_phone(phone_e164) if phone_e164 is not None else None
    )
    email_match: HubSpotContactSnapshot | None = None
    email_complete = True
    if trusted_email is not None:
        try:
            email_match = await client.get_contact_by_email(trusted_email)
        except HubSpotProviderError as error:
            if error.code is not HubSpotErrorCode.NOT_FOUND:
                raise
    return decide_crm_contact_identity(
        local_phone_e164=phone_e164,
        trusted_verified_email=trusted_email,
        phone_search=phone_search,
        email_match=email_match,
        email_lookup_complete=email_complete,
        default_region=default_region,
    )


async def _verify_active_link(
    client: HubSpotClient,
    snapshot: _ResolutionSnapshot,
    provider: HubSpotContactSnapshot,
    *,
    trusted_email: str | None,
    replayed: bool,
) -> CRMContactResolutionResult | None:
    link = snapshot.link
    if link is None:
        return _identity_conflict(replayed)

    if link.linked_by is CRMContactLinkProvenance.PHONE_E164:
        if snapshot.phone_e164 is None:
            return _result(
                CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
                replayed=replayed,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )
        phone_status = _provider_phone_status(
            provider,
            snapshot.phone_e164,
            snapshot.default_phone_region or "",
        )
        if phone_status == "missing":
            return _result(
                CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
                replayed=replayed,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )
        if phone_status == "conflict":
            return _identity_conflict(replayed)
        if phone_status == "invalid":
            return _ambiguous(replayed)
    else:
        persisted_email = _normalized_email(snapshot.local_email)
        provider_email = _normalized_email(provider.email)
        if persisted_email is None or provider_email is None:
            return _result(
                CRMContactResolutionOutcome.UNRESOLVED_IDENTITY,
                replayed=replayed,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            )
        if provider_email is _INVALID_EMAIL:
            return _ambiguous(replayed)
        if provider_email != persisted_email:
            return _identity_conflict(replayed)

    provider_email = _normalized_email(provider.email)
    if provider_email is _INVALID_EMAIL:
        return _ambiguous(replayed)
    if (
        trusted_email is not None
        and provider_email is not None
        and provider_email != _normalized_email(trusted_email)
    ):
        return _identity_conflict(replayed)

    if (
        snapshot.phone_e164 is not None
        and link.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL
    ):
        phone_search = await client.search_contacts_by_phone(snapshot.phone_e164)
        phone_decision = decide_crm_contact_identity(
            local_phone_e164=snapshot.phone_e164,
            trusted_verified_email=None,
            phone_search=phone_search,
            email_match=None,
            email_lookup_complete=True,
            default_region=snapshot.default_phone_region or "",
        )
        if phone_decision.outcome is CRMContactIdentityOutcome.AMBIGUOUS:
            return _ambiguous(replayed)
        if (
            phone_decision.outcome is CRMContactIdentityOutcome.MATCHED
            and phone_decision.hubspot_contact_id != provider.id
        ):
            return _identity_conflict(replayed)
        if phone_decision.outcome is CRMContactIdentityOutcome.NOT_FOUND:
            provider_phone_status = _provider_phone_status(
                provider,
                snapshot.phone_e164,
                snapshot.default_phone_region or "",
            )
            if provider_phone_status == "conflict":
                return _identity_conflict(replayed)
            if provider_phone_status == "invalid":
                return _ambiguous(replayed)

    if trusted_email is not None and link.linked_by is CRMContactLinkProvenance.PHONE_E164:
        try:
            email_match = await client.get_contact_by_email(trusted_email)
        except HubSpotProviderError as error:
            if error.code is not HubSpotErrorCode.NOT_FOUND:
                raise
            email_match = None
        if email_match is not None:
            actual_email = _normalized_email(email_match.email)
            if actual_email is _INVALID_EMAIL:
                return _ambiguous(replayed)
            if actual_email is None:
                return _ambiguous(replayed)
            if actual_email != _normalized_email(trusted_email):
                return _identity_conflict(replayed)
            if email_match.id != provider.id:
                return _identity_conflict(replayed)
    return None


async def _mark_link_stale(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _ResolutionSnapshot,
) -> CRMContactResolutionResult | None:
    if snapshot.link is None:
        return None
    try:
        async with session_factory.begin() as session:
            contact, conversation = await _locked_owner(session, snapshot)
            if contact is None or conversation is None:
                return _local_state_changed(snapshot.replayed)
            if not _same_contact_state(contact, snapshot):
                return _local_state_changed(snapshot.replayed)
            link = await session.scalar(
                select(CRMContactLink)
                .where(CRMContactLink.contact_id == snapshot.contact_id)
                .with_for_update()
            )
            if link is None or not _same_link(link, snapshot.link):
                return _local_state_changed(snapshot.replayed)
            link.state = CRMContactLinkState.STALE
    except SQLAlchemyError:
        return _persistence_failure(snapshot.replayed)
    return None


async def _apply_verified_link(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _ResolutionSnapshot,
    request: CRMContactResolutionRequest,
    settings: Settings,
    expected_portal_id: str | None,
    service_key: object,
    *,
    provider_id: str,
    provenance: CRMContactLinkProvenance,
    trusted_email: str | None,
    relinking: bool,
) -> CRMContactResolutionResult:
    if not _settings_unchanged(settings, service_key, expected_portal_id):
        return await _finish(
            session_factory,
            snapshot,
            _result(
                CRMContactResolutionOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMResolutionErrorCode.ACCOUNT_MISMATCH,
                next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
            ),
        )

    outcome = (
        CRMContactResolutionOutcome.FOUND_AND_LINKED
        if snapshot.link is None or relinking
        else CRMContactResolutionOutcome.LINKED
    )
    success_result = _result(
        outcome,
        provider_id=provider_id,
        replayed=snapshot.replayed,
    )
    try:
        async with session_factory.begin() as session:
            contact, conversation = await _locked_owner(session, snapshot)
            if (
                contact is None
                or conversation is None
                or not _same_contact_state(contact, snapshot, trusted_email=trusted_email)
            ):
                result = _local_state_changed(snapshot.replayed)
                await _terminalize(session, snapshot, result)
                return result
            business = await session.get(BusinessConfig, 1)
            if (
                business is None
                or business.default_phone_region != snapshot.default_phone_region
                or not _settings_unchanged(settings, service_key, expected_portal_id)
            ):
                result = _local_state_changed(snapshot.replayed)
                await _terminalize(session, snapshot, result)
                return result

            link = await session.scalar(
                select(CRMContactLink)
                .where(CRMContactLink.contact_id == snapshot.contact_id)
                .with_for_update()
            )
            if snapshot.link is None:
                if link is not None and not _same_link_decision(
                    link,
                    portal_id=expected_portal_id,
                    provider_id=provider_id,
                    provenance=provenance,
                ):
                    result = _local_state_changed(snapshot.replayed)
                    await _terminalize(session, snapshot, result)
                    return result
            elif link is None or (
                not _same_link(link, snapshot.link)
                and not _same_link_decision(
                    link,
                    portal_id=expected_portal_id,
                    provider_id=provider_id,
                    provenance=provenance,
                )
            ):
                result = _local_state_changed(snapshot.replayed)
                await _terminalize(session, snapshot, result)
                return result

            competing_link = await session.scalar(
                select(CRMContactLink)
                .where(
                    CRMContactLink.hubspot_portal_id == expected_portal_id,
                    CRMContactLink.hubspot_contact_id == provider_id,
                    CRMContactLink.contact_id != snapshot.contact_id,
                )
                .with_for_update()
            )
            if competing_link is not None:
                result = _identity_conflict(snapshot.replayed)
                await _terminalize(session, snapshot, result)
                return result

            if trusted_email is not None:
                if contact.email is not None and _normalized_email(
                    contact.email
                ) != _normalized_email(trusted_email):
                    result = _identity_conflict(snapshot.replayed)
                    await _terminalize(session, snapshot, result)
                    return result
                if contact.email is None:
                    contact.email = trusted_email

            if link is None:
                link = CRMContactLink(
                    contact_id=snapshot.contact_id,
                    hubspot_portal_id=expected_portal_id or "",
                    hubspot_contact_id=provider_id,
                    linked_by=provenance,
                    state=CRMContactLinkState.ACTIVE,
                    verified_at=datetime.now(UTC),
                )
                session.add(link)
            else:
                link.hubspot_portal_id = expected_portal_id or ""
                link.hubspot_contact_id = provider_id
                link.linked_by = provenance
                link.state = CRMContactLinkState.ACTIVE
                link.verified_at = datetime.now(UTC)

            await _terminalize(session, snapshot, success_result)
    except _TerminalizationError:
        return _persistence_failure(snapshot.replayed)
    except IntegrityError:
        try:
            conflict = await _provider_link_conflict(
                session_factory,
                portal_id=expected_portal_id,
                provider_id=provider_id,
                contact_id=snapshot.contact_id,
            )
        except SQLAlchemyError:
            return _persistence_failure(snapshot.replayed)
        result = (
            _identity_conflict(snapshot.replayed)
            if conflict
            else _persistence_failure(snapshot.replayed)
        )
        return await _finish(session_factory, snapshot, result)
    except SQLAlchemyError:
        return await _finish(session_factory, snapshot, _persistence_failure(snapshot.replayed))
    return success_result


async def _provider_link_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    portal_id: str | None,
    provider_id: str,
    contact_id: UUID,
) -> bool:
    if portal_id is None:
        return False
    async with session_factory() as session:
        competing = await session.scalar(
            select(CRMContactLink).where(
                CRMContactLink.hubspot_portal_id == portal_id,
                CRMContactLink.hubspot_contact_id == provider_id,
                CRMContactLink.contact_id != contact_id,
            )
        )
        return competing is not None


async def _locked_owner(
    session: AsyncSession, snapshot: _ResolutionSnapshot
) -> tuple[Contact | None, Conversation | None]:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == snapshot.conversation_id).with_for_update()
    )
    contact = await session.scalar(
        select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
    )
    if conversation is None or contact is None or conversation.contact_id != contact.id:
        return None, None
    return contact, conversation


def _same_contact_state(
    contact: Contact,
    snapshot: _ResolutionSnapshot,
    *,
    trusted_email: str | None = None,
) -> bool:
    email_unchanged = contact.email == snapshot.local_email
    email_persisted_by_parallel_resolution = (
        snapshot.local_email is None
        and trusted_email is not None
        and _normalized_email(contact.email) == _normalized_email(trusted_email)
    )
    return contact.phone_e164 == snapshot.phone_e164 and (
        email_unchanged or email_persisted_by_parallel_resolution
    )


def _same_link(link: CRMContactLink, expected: _LinkSnapshot) -> bool:
    return (
        link.hubspot_portal_id == expected.portal_id
        and link.hubspot_contact_id == expected.hubspot_contact_id
        and link.linked_by is expected.linked_by
        and link.state is expected.state
    )


def _same_link_decision(
    link: CRMContactLink,
    *,
    portal_id: str | None,
    provider_id: str,
    provenance: CRMContactLinkProvenance,
) -> bool:
    return (
        link.hubspot_portal_id == portal_id
        and link.hubspot_contact_id == provider_id
        and link.linked_by is provenance
        and link.state is CRMContactLinkState.ACTIVE
    )


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _ResolutionSnapshot,
    result: CRMContactResolutionResult,
) -> CRMContactResolutionResult:
    try:
        async with session_factory.begin() as session:
            await _terminalize(session, snapshot, result)
    except (SQLAlchemyError, _TerminalizationError):
        return _persistence_failure(snapshot.replayed)
    return result


async def _terminalize(
    session: AsyncSession,
    snapshot: _ResolutionSnapshot,
    result: CRMContactResolutionResult,
) -> None:
    execution = await session.get(ToolExecution, snapshot.execution_id)
    if execution is None:
        raise _TerminalizationError
    execution.status = _execution_status(result.outcome)
    execution.sanitized_result = result.model_dump(mode="json")
    execution.safe_error_code = None if result.error_code is None else result.error_code.value
    execution.safe_error_message = (
        None
        if result.error_code is None
        else "CRM Contact resolution did not complete successfully"
    )
    execution.finished_at = datetime.now(UTC)
    audit_outcome = "succeeded" if execution.status is ToolExecutionStatus.SUCCEEDED else "rejected"
    if result.replayed and execution.status is ToolExecutionStatus.SUCCEEDED:
        audit_outcome = "reconciled"
    session.add(
        AuditEvent(
            event_type=_OPERATION,
            actor_type=AuditActorType.SYSTEM,
            contact_id=snapshot.contact_id,
            conversation_id=snapshot.conversation_id,
            entity_type="crm_contact_link",
            entity_identifier=(
                result.hubspot_contact_id
                if result.hubspot_contact_id is not None
                else str(snapshot.contact_id)
            ),
            sanitized_metadata={
                "operation": _OPERATION,
                "outcome": audit_outcome,
                "tool_execution_id": str(snapshot.execution_id),
                "replayed": result.replayed,
            },
        )
    )


def _execution_status(outcome: CRMContactResolutionOutcome) -> ToolExecutionStatus:
    if outcome in {
        CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
    }:
        return ToolExecutionStatus.FAILED
    if outcome in {
        CRMContactResolutionOutcome.NOT_CONFIGURED,
        CRMContactResolutionOutcome.PERMISSION_DENIED,
        CRMContactResolutionOutcome.INVALID_REQUEST,
        CRMContactResolutionOutcome.IDENTITY_CONFLICT,
    }:
        return ToolExecutionStatus.REJECTED
    return ToolExecutionStatus.SUCCEEDED


def _provider_error_result(
    error: HubSpotProviderError,
    *,
    replayed: bool,
) -> CRMContactResolutionResult:
    if error.code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}:
        return _result(
            CRMContactResolutionOutcome.PERMISSION_DENIED,
            replayed=replayed,
            error_code=CRMResolutionErrorCode.PERMISSION_DENIED,
            next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
        )
    if error.code is HubSpotErrorCode.NOT_CONFIGURED:
        return _result(
            CRMContactResolutionOutcome.NOT_CONFIGURED,
            replayed=replayed,
            error_code=CRMResolutionErrorCode.NOT_CONFIGURED,
            next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
        )
    code = (
        CRMResolutionErrorCode.INVALID_PROVIDER_RESPONSE
        if error.code is HubSpotErrorCode.INVALID_RESPONSE
        else CRMResolutionErrorCode.PROVIDER_UNAVAILABLE
    )
    return _result(
        CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
        replayed=replayed,
        error_code=code,
        next_action=CRMResolutionNextAction.RETRY_SAME_KEY,
    )


def _preflight_failure(
    result: HubSpotPreflightResult,
    *,
    replayed: bool,
) -> CRMContactResolutionResult | None:
    if (
        result.outcome is HubSpotPreflightOutcome.VERIFIED
        and result.account_identity_verified
        and result.capability_status is HubSpotCapabilityStatus.UNVERIFIED
    ):
        return None
    if result.outcome is HubSpotPreflightOutcome.PERMISSION_DENIED:
        return _result(
            CRMContactResolutionOutcome.PERMISSION_DENIED,
            replayed=replayed,
            error_code=CRMResolutionErrorCode.PERMISSION_DENIED,
            next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
        )
    if result.outcome is HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE:
        return _result(
            CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
            replayed=replayed,
            error_code=CRMResolutionErrorCode.PROVIDER_UNAVAILABLE,
            next_action=CRMResolutionNextAction.RETRY_SAME_KEY,
        )
    code = (
        CRMResolutionErrorCode.ACCOUNT_MISMATCH
        if result.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH
        else CRMResolutionErrorCode.INVALID_PROVIDER_RESPONSE
        if result.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE
        else CRMResolutionErrorCode.NOT_CONFIGURED
    )
    return _result(
        CRMContactResolutionOutcome.NOT_CONFIGURED,
        replayed=replayed,
        error_code=code,
        next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
    )


def _decision_outcome(outcome: CRMContactIdentityOutcome) -> CRMContactResolutionOutcome:
    return {
        CRMContactIdentityOutcome.MATCHED: CRMContactResolutionOutcome.FOUND_AND_LINKED,
        CRMContactIdentityOutcome.NOT_FOUND: CRMContactResolutionOutcome.NOT_FOUND,
        CRMContactIdentityOutcome.AMBIGUOUS: CRMContactResolutionOutcome.AMBIGUOUS,
        CRMContactIdentityOutcome.IDENTITY_CONFLICT: CRMContactResolutionOutcome.IDENTITY_CONFLICT,
        CRMContactIdentityOutcome.UNRESOLVED_IDENTITY: (
            CRMContactResolutionOutcome.UNRESOLVED_IDENTITY
        ),
    }[outcome]


def _provider_phone_status(
    snapshot: HubSpotContactSnapshot,
    local_phone: str,
    default_region: str,
) -> Literal["matched", "missing", "conflict", "invalid"]:
    values: list[str] = []
    for raw_value in (snapshot.phone, snapshot.mobilephone):
        if raw_value is None or not raw_value.strip():
            continue
        try:
            values.append(normalize_phone_number(raw_value, default_region))
        except PhoneNormalizationError:
            return "invalid"
    if local_phone in values:
        return "matched"
    return "conflict" if values else "missing"


class _InvalidEmail:
    pass


_INVALID_EMAIL = _InvalidEmail()


def _normalized_email(value: str | None) -> str | _InvalidEmail | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if not normalized:
        return None
    if (
        len(normalized) > _EMAIL_LIMIT
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        return _INVALID_EMAIL
    return normalized


def _identity_conflict(replayed: bool = False) -> CRMContactResolutionResult:
    return _result(
        CRMContactResolutionOutcome.IDENTITY_CONFLICT,
        replayed=replayed,
        error_code=CRMResolutionErrorCode.IDENTITY_CONFLICT,
        next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
    )


def _ambiguous(replayed: bool = False) -> CRMContactResolutionResult:
    return _result(
        CRMContactResolutionOutcome.AMBIGUOUS,
        replayed=replayed,
        next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
    )


def _local_state_changed(replayed: bool) -> CRMContactResolutionResult:
    return _result(
        CRMContactResolutionOutcome.IDENTITY_CONFLICT,
        replayed=replayed,
        error_code=CRMResolutionErrorCode.LOCAL_STATE_CHANGED,
        next_action=CRMResolutionNextAction.OPERATOR_REVIEW,
    )


def _persistence_failure(replayed: bool) -> CRMContactResolutionResult:
    return _result(
        CRMContactResolutionOutcome.PROVIDER_UNAVAILABLE,
        replayed=replayed,
        error_code=CRMResolutionErrorCode.LOCAL_PERSISTENCE_FAILURE,
        next_action=CRMResolutionNextAction.RECONCILE_SAME_KEY,
    )


def _settings_configured(settings: Settings) -> bool:
    key = settings.hubspot_service_key
    portal_id = settings.hubspot_portal_id
    return (
        key is not None
        and bool(key.get_secret_value().strip())
        and portal_id is not None
        and portal_id.isascii()
        and portal_id.isdecimal()
        and int(portal_id) > 0
    )


def _settings_unchanged(
    settings: Settings,
    expected_key: object,
    expected_portal_id: str | None,
) -> bool:
    return (
        settings.hubspot_service_key is expected_key
        and settings.hubspot_portal_id == expected_portal_id
        and _settings_configured(settings)
    )

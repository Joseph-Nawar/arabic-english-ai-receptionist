"""Explicit, recoverable HubSpot Contact updates with a fixed field allowlist."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.application.booking import ToolExecutionClaimOutcome, _claim_tool_execution
from receptionist.application.crm_contact_writes import (
    CRMContactUpdateOutcome,
    CRMContactUpdateRequest,
    CRMContactUpdateResult,
    CRMContactWriteErrorCode,
    CRMContactWriteNextAction,
    CRMContactWriteValidationOutcome,
    CRMContactWriteValidationResult,
    contact_identity_conflicts,
    validate_crm_contact_write,
)
from receptionist.application.crm_identity import (
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
    HubSpotClient,
    HubSpotContactSearchResult,
    HubSpotContactSnapshot,
    HubSpotErrorCode,
    HubSpotPreflightOutcome,
    HubSpotPreflightResult,
    HubSpotProviderError,
)

_OPERATION: Literal["update_crm_contact"] = "update_crm_contact"
_CRM_MUTATIONS = ("create_crm_contact", "update_crm_contact", "record_booking_outcome")


@dataclass(frozen=True, slots=True)
class _UpdateFieldPlan:
    patch_email: str | None = field(default=None, repr=False)
    patch_phone: str | None = field(default=None, repr=False)
    conflict: bool = False
    error_code: CRMContactWriteErrorCode | None = None


@dataclass(frozen=True, slots=True)
class _UpdateSnapshot:
    contact_id: UUID
    conversation_id: UUID
    phone_e164: str | None
    local_email: str | None = field(repr=False)
    default_phone_region: str
    portal_id: str
    hubspot_contact_id: str
    linked_by: CRMContactLinkProvenance
    link_state: CRMContactLinkState | None
    execution_id: UUID
    replayed: bool
    recovery: bool
    bound_key: SecretStr = field(repr=False)
    selected_email: str | None = field(repr=False)
    selected_phone: str | None = field(repr=False)


async def update_crm_contact(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactUpdateRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CRMContactUpdateResult:
    """Apply an internal fill-blank update, with claim-before-PATCH recovery.

    The request does not authenticate its caller. Trusted provenance must be
    established by a separate controlled boundary. The existing HubSpot
    capability guard remains authoritative for production writes.
    """
    try:
        claimed = await _claim_update(session_factory, request, settings)
    except SQLAlchemyError:
        return _result(
            CRMContactUpdateOutcome.FAILED,
            error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if isinstance(claimed, CRMContactUpdateResult):
        return claimed
    validation = await validate_crm_contact_write(session_factory, request)
    if validation.outcome is not CRMContactWriteValidationOutcome.VALID:
        return await _finish_or_retain(session_factory, claimed, _validation_failure(validation))
    return await _run_claimed_update(session_factory, claimed, request, settings, transport)


async def _claim_update(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactUpdateRequest,
    settings: Settings,
) -> _UpdateSnapshot | CRMContactUpdateResult:
    durable_key = _durable_key(request.idempotency_key)
    email_tag, phone_tag = _evidence_tags(request)
    async with session_factory.begin() as session:
        contact = await session.scalar(
            select(Contact).where(Contact.id == request.contact_id).with_for_update()
        )
        conversation = await session.get(Conversation, request.conversation_id)
        if contact is None or conversation is None:
            return _result(
                CRMContactUpdateOutcome.INVALID_REQUEST,
                error_code=CRMContactWriteErrorCode.INVALID_REQUEST,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        if conversation.contact_id != contact.id:
            return _result(
                CRMContactUpdateOutcome.OWNERSHIP_REJECTED,
                error_code=CRMContactWriteErrorCode.OWNERSHIP_MISMATCH,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        business = await session.get(BusinessConfig, 1)
        if business is None:
            return _result(
                CRMContactUpdateOutcome.NOT_CONFIGURED,
                error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        if not _settings_configured(settings):
            return _result(
                CRMContactUpdateOutcome.NOT_CONFIGURED,
                error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        bound_key = settings.hubspot_service_key
        if bound_key is None:
            return _result(
                CRMContactUpdateOutcome.NOT_CONFIGURED,
                error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        existing = await session.scalar(
            select(ToolExecution)
            .where(ToolExecution.idempotency_key == durable_key)
            .with_for_update()
        )
        if existing is not None and existing.tool_name != _OPERATION:
            return _result(
                CRMContactUpdateOutcome.FAILED,
                replayed=True,
                error_code=CRMContactWriteErrorCode.IDEMPOTENCY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        link = await session.get(CRMContactLink, contact.id)
        provider_id: str
        provenance: CRMContactLinkProvenance
        if existing is None:
            if link is None or link.state is not CRMContactLinkState.ACTIVE:
                return _result(
                    CRMContactUpdateOutcome.CONFLICT,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            if link.hubspot_portal_id != settings.hubspot_portal_id:
                return _result(
                    CRMContactUpdateOutcome.NOT_CONFIGURED,
                    error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            provider_id = link.hubspot_contact_id
            provenance = link.linked_by
        else:
            stored = existing.sanitized_arguments
            stored_provider_id = (
                stored.get("hubspot_contact_id") if isinstance(stored, dict) else None
            )
            provenance_value = stored.get("linked_by") if isinstance(stored, dict) else None
            if not isinstance(provenance_value, str):
                return _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            try:
                provenance = CRMContactLinkProvenance(provenance_value)
            except (TypeError, ValueError):
                return _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            if not isinstance(stored_provider_id, str) or not stored_provider_id:
                return _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            provider_id = stored_provider_id

        arguments: dict[str, object] = {
            "conversation_id": str(request.conversation_id),
            "contact_id": str(request.contact_id),
            "hubspot_portal_id": settings.hubspot_portal_id,
            "hubspot_contact_id": provider_id,
            "linked_by": provenance.value,
            "email_selected": request.trusted_verified_email is not None,
            "email_identity_tag": email_tag,
            "phone_selected": request.trusted_verified_phone is not None,
            "phone_identity_tag": phone_tag,
        }
        if existing is None:
            active = await session.scalar(
                select(ToolExecution.id)
                .where(
                    ToolExecution.conversation_id.in_(
                        select(Conversation.id).where(Conversation.contact_id == contact.id)
                    ),
                    ToolExecution.tool_name.in_(
                        ("create_crm_contact", "update_crm_contact", "record_booking_outcome")
                    ),
                    ToolExecution.status == ToolExecutionStatus.STARTED,
                )
                .limit(1)
            )
            if active is not None:
                return _result(
                    CRMContactUpdateOutcome.FAILED,
                    error_code=CRMContactWriteErrorCode.OPERATION_IN_PROGRESS,
                    next_action=CRMContactWriteNextAction.RETRY_SAME_KEY,
                )

        claim = await _claim_tool_execution(
            session,
            conversation_id=request.conversation_id,
            tool_name=_OPERATION,
            idempotency_key=durable_key,
            sanitized_arguments=arguments,
        )
        if claim.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT:
            return _result(
                CRMContactUpdateOutcome.FAILED,
                replayed=existing is not None,
                error_code=CRMContactWriteErrorCode.IDEMPOTENCY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        if existing is not None:
            if claim.outcome in {
                ToolExecutionClaimOutcome.SUCCEEDED_REPLAY,
                ToolExecutionClaimOutcome.REJECTED_REPLAY,
            }:
                return _stored_replay(claim.execution)
            if claim.outcome is ToolExecutionClaimOutcome.FAILED_REPLAY:
                if not _retryable_failure(claim.execution):
                    return _stored_replay(claim.execution)
                claim.execution.status = ToolExecutionStatus.STARTED
                claim.execution.sanitized_result = None
                claim.execution.safe_error_code = None
                claim.execution.safe_error_message = None
                claim.execution.finished_at = None
                recovery = False
                replayed = True
            else:
                recovery = True
                replayed = True
        else:
            if claim.outcome is ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS:
                return _result(
                    CRMContactUpdateOutcome.FAILED,
                    error_code=CRMContactWriteErrorCode.OPERATION_IN_PROGRESS,
                    next_action=CRMContactWriteNextAction.RETRY_SAME_KEY,
                )
            if claim.outcome is not ToolExecutionClaimOutcome.NEW:
                return _stored_replay(claim.execution)
            recovery = False
            replayed = False

        snapshot = _UpdateSnapshot(
            contact_id=contact.id,
            conversation_id=conversation.id,
            phone_e164=contact.phone_e164,
            local_email=contact.email,
            default_phone_region=business.default_phone_region,
            portal_id=settings.hubspot_portal_id or "",
            hubspot_contact_id=provider_id,
            linked_by=provenance,
            link_state=None if link is None else link.state,
            execution_id=claim.execution.id,
            replayed=replayed,
            recovery=recovery,
            bound_key=bound_key,
            selected_email=request.trusted_verified_email,
            selected_phone=request.trusted_verified_phone,
        )
        if not recovery:
            session.add(_audit(snapshot, "started"))
        if contact_identity_conflicts(contact, request):
            if recovery:
                result = _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                claim.execution.status = ToolExecutionStatus.STARTED
                claim.execution.sanitized_result = result.model_dump(mode="json")
                claim.execution.safe_error_code = (
                    None if result.error_code is None else result.error_code.value
                )
                claim.execution.safe_error_message = (
                    "CRM Contact update requires operator identity review"
                )
                claim.execution.finished_at = None
                session.add(_audit(snapshot, "uncertain"))
                return result
            result = _result(
                CRMContactUpdateOutcome.CONFLICT,
                replayed=replayed,
                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
            _terminalize(session, claim.execution, snapshot, result)
            return result
        return snapshot


async def _run_claimed_update(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    request: CRMContactUpdateRequest,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None,
) -> CRMContactUpdateResult:
    if not _settings_unchanged(settings, snapshot):
        failure = _result(
            CRMContactUpdateOutcome.NOT_CONFIGURED,
            replayed=snapshot.replayed,
            error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
        return await _finish_or_retain(session_factory, snapshot, failure)
    try:
        preflight_client = HubSpotClient(settings, transport=transport)
    except HubSpotProviderError as error:
        return await _provider_failure(session_factory, snapshot, error, before_write=True)

    operation_client: HubSpotClient | None = None
    try:
        preflight = await preflight_client.preflight_account()
        preflight_failure = _preflight_failure(preflight, replayed=snapshot.replayed)
        if preflight_failure is not None:
            return await _finish_or_retain(session_factory, snapshot, preflight_failure)
        if not _settings_unchanged(settings, snapshot):
            failure = _result(
                CRMContactUpdateOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
            return await _finish_or_retain(session_factory, snapshot, failure)

        mock_only = settings.app_env in {"test", "ci"} and isinstance(
            transport, httpx.MockTransport
        )
        if preflight.writes_enabled:
            operation_client = preflight_client
        elif mock_only:
            # M3A's original client intentionally leaves mock preflight from
            # enabling capability. The second client is test transport only.
            operation_client = HubSpotClient(settings, transport=transport)
        else:
            failure = _result(
                CRMContactUpdateOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.ACTIVATION_REQUIRED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
            return await _finish_or_retain(session_factory, snapshot, failure)

        if not await _current_link(session_factory, snapshot):
            return await _identity_failure(session_factory, snapshot)
        try:
            provider = await operation_client.get_contact_by_id(snapshot.hubspot_contact_id)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.NOT_FOUND:
                await _mark_stale(session_factory, snapshot)
                return await _identity_failure(session_factory, snapshot)
            return await _provider_failure(session_factory, snapshot, error, before_write=True)
        if provider.archived or provider.id != snapshot.hubspot_contact_id:
            await _mark_stale(session_factory, snapshot)
            return await _identity_failure(session_factory, snapshot)

        identity_error = await _verify_provider_identity(
            operation_client, snapshot, request, provider
        )
        if identity_error is not None:
            return await _finish_or_retain(session_factory, snapshot, identity_error)
        plan = _plan_update_fields(request, provider, default_region=snapshot.default_phone_region)
        if plan.conflict:
            return await _finish_or_retain(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.UNCERTAIN
                    if snapshot.recovery
                    else CRMContactUpdateOutcome.CONFLICT,
                    replayed=snapshot.replayed,
                    error_code=plan.error_code or CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        if snapshot.recovery:
            if plan.patch_email is None and plan.patch_phone is None:
                return await _persist_success(
                    session_factory,
                    snapshot,
                    request,
                    updated_fields=_selected_field_names(request),
                )
            return await _record_uncertain(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE,
                    next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                ),
            )

        if plan.patch_email is None and plan.patch_phone is None:
            return await _persist_success(session_factory, snapshot, request)
        if not await _local_state_unchanged(session_factory, snapshot, settings):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.CONFLICT,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        # Read immediately before PATCH to catch visible staff edits. HubSpot
        # offers no compare-and-swap guarantee, so a later race remains possible.
        try:
            latest = await operation_client.get_contact_by_id(snapshot.hubspot_contact_id)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.NOT_FOUND:
                await _mark_stale(session_factory, snapshot)
                return await _finish(
                    session_factory,
                    snapshot,
                    _result(
                        CRMContactUpdateOutcome.CONFLICT,
                        error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                        next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                    ),
                )
            return await _provider_failure(session_factory, snapshot, error, before_write=True)
        if latest.archived or latest.id != snapshot.hubspot_contact_id:
            await _mark_stale(session_factory, snapshot)
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.CONFLICT,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )
        identity_error = await _verify_provider_identity(
            operation_client, snapshot, request, latest
        )
        if identity_error is not None:
            return await _finish(session_factory, snapshot, identity_error)
        final_plan = _plan_update_fields(
            request, latest, default_region=snapshot.default_phone_region
        )
        if final_plan.conflict:
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.CONFLICT,
                    error_code=final_plan.error_code or CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )
        if final_plan.patch_email is None and final_plan.patch_phone is None:
            return await _persist_success(session_factory, snapshot, request)
        if not await _local_state_unchanged(session_factory, snapshot, settings):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.CONFLICT,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )
        if not _settings_unchanged(settings, snapshot):
            return await _finish(
                session_factory,
                snapshot,
                _result(
                    CRMContactUpdateOutcome.NOT_CONFIGURED,
                    error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        try:
            updated = await operation_client.update_contact(
                snapshot.hubspot_contact_id,
                email=final_plan.patch_email,
                phone=final_plan.patch_phone,
            )
        except HubSpotProviderError as error:
            if error.outcome_uncertain:
                if error.code in {
                    HubSpotErrorCode.INVALID_RESPONSE,
                    HubSpotErrorCode.RESPONSE_TOO_LARGE,
                }:
                    reconciled = await _read_for_reconciliation(operation_client, snapshot, request)
                    if reconciled:
                        return await _persist_success(
                            session_factory,
                            snapshot,
                            request,
                            updated_fields=_plan_field_names(final_plan),
                        )
                return await _record_uncertain(
                    session_factory,
                    snapshot,
                    _provider_failure_result(error, replayed=False, uncertain=True),
                )
            if error.code is HubSpotErrorCode.NOT_FOUND:
                await _mark_stale(session_factory, snapshot)
                return await _finish(
                    session_factory,
                    snapshot,
                    _result(
                        CRMContactUpdateOutcome.CONFLICT,
                        error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                        next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                    ),
                )
            return await _provider_failure(session_factory, snapshot, error, before_write=False)

        accepted_is_verified = (
            updated.id == snapshot.hubspot_contact_id
            and not updated.archived
            and _selected_values_equal(request, updated, snapshot.default_phone_region)
        )
        if accepted_is_verified:
            try:
                identity_error = await _verify_provider_identity(
                    operation_client, snapshot, request, updated
                )
            except HubSpotProviderError as error:
                return await _record_uncertain(
                    session_factory,
                    snapshot,
                    _provider_failure_result(error, replayed=False, uncertain=True),
                )
            if identity_error is None:
                return await _persist_success(
                    session_factory,
                    snapshot,
                    request,
                    updated_fields=_plan_field_names(final_plan),
                )

        # The PATCH may have succeeded despite an unusable response. Only a
        # read is permitted here; same-key recovery must never repeat the PATCH.
        try:
            reconciled_contact = await operation_client.get_contact_by_id(
                snapshot.hubspot_contact_id
            )
            if (
                reconciled_contact.id == snapshot.hubspot_contact_id
                and not reconciled_contact.archived
                and _selected_values_equal(
                    request, reconciled_contact, snapshot.default_phone_region
                )
                and await _verify_provider_identity(
                    operation_client, snapshot, request, reconciled_contact
                )
                is None
            ):
                return await _persist_success(
                    session_factory,
                    snapshot,
                    request,
                    updated_fields=_plan_field_names(final_plan),
                )
        except HubSpotProviderError:
            pass
        return await _record_uncertain(
            session_factory,
            snapshot,
            _result(
                CRMContactUpdateOutcome.UNCERTAIN,
                error_code=CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )
    except HubSpotProviderError as error:
        return await _provider_failure(
            session_factory,
            snapshot,
            error,
            before_write=not snapshot.recovery,
        )
    except (IntegrityError, SQLAlchemyError):
        return await _record_uncertain(
            session_factory,
            snapshot,
            _result(
                CRMContactUpdateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )
    finally:
        if operation_client is not None and operation_client is not preflight_client:
            await operation_client.aclose()
        await preflight_client.aclose()


async def _read_for_reconciliation(
    client: HubSpotClient,
    snapshot: _UpdateSnapshot,
    request: CRMContactUpdateRequest,
) -> bool:
    try:
        provider = await client.get_contact_by_id(snapshot.hubspot_contact_id)
        if (
            provider.id != snapshot.hubspot_contact_id
            or provider.archived
            or not _selected_values_equal(request, provider, snapshot.default_phone_region)
        ):
            return False
        return await _verify_provider_identity(client, snapshot, request, provider) is None
    except HubSpotProviderError:
        return False


async def _verify_provider_identity(
    client: HubSpotClient,
    snapshot: _UpdateSnapshot,
    request: CRMContactUpdateRequest,
    provider: HubSpotContactSnapshot,
) -> CRMContactUpdateResult | None:
    if provider.id != snapshot.hubspot_contact_id or provider.archived:
        return _conflict_result(snapshot, CRMContactWriteErrorCode.IDENTITY_CONFLICT)
    provider_email = _normalize_email(provider.email)
    if provider_email is _INVALID_EMAIL:
        return _conflict_result(snapshot, CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE)
    provider_phones = tuple(
        _normalize_phone(value, snapshot.default_phone_region)
        for value in (provider.phone, provider.mobilephone)
    )
    if _INVALID_PHONE in provider_phones:
        return _conflict_result(snapshot, CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE)

    if snapshot.linked_by is CRMContactLinkProvenance.PHONE_E164:
        expected_phone = _normalize_phone(snapshot.phone_e164, snapshot.default_phone_region)
        if not isinstance(expected_phone, str) or expected_phone not in provider_phones:
            return _conflict_result(snapshot, CRMContactWriteErrorCode.IDENTITY_CONFLICT)
    else:
        expected_email = _normalize_email(snapshot.local_email)
        if (
            not isinstance(expected_email, str)
            or not isinstance(provider_email, str)
            or provider_email != expected_email
        ):
            return _conflict_result(snapshot, CRMContactWriteErrorCode.IDENTITY_CONFLICT)
        if snapshot.phone_e164 is not None:
            known_phones = tuple(value for value in provider_phones if isinstance(value, str))
            if known_phones and snapshot.phone_e164 not in known_phones:
                return _conflict_result(snapshot, CRMContactWriteErrorCode.IDENTITY_CONFLICT)

    identity_phone = snapshot.phone_e164 or request.trusted_verified_phone
    identity_email = request.trusted_verified_email or (
        snapshot.local_email
        if snapshot.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL
        else None
    )
    phone_search: HubSpotContactSearchResult | None = None
    if identity_phone is not None:
        phone_search = await client.search_contacts_by_phone(identity_phone)
    email_match: HubSpotContactSnapshot | None = None
    if identity_email is not None:
        try:
            email_match = await client.get_contact_by_email(identity_email)
        except HubSpotProviderError as error:
            if error.code is not HubSpotErrorCode.NOT_FOUND:
                raise
    decision = decide_crm_contact_identity(
        local_phone_e164=identity_phone,
        trusted_verified_email=identity_email,
        phone_search=phone_search,
        email_match=email_match,
        email_lookup_complete=True,
        default_region=snapshot.default_phone_region,
    )
    if (
        decision.outcome is CRMContactIdentityOutcome.MATCHED
        and decision.hubspot_contact_id == snapshot.hubspot_contact_id
    ):
        return None
    if decision.outcome is CRMContactIdentityOutcome.AMBIGUOUS:
        code = CRMContactWriteErrorCode.AMBIGUOUS_IDENTITY
    else:
        code = CRMContactWriteErrorCode.IDENTITY_CONFLICT
    return _conflict_result(snapshot, code)


def _plan_update_fields(
    request: CRMContactUpdateRequest,
    provider: HubSpotContactSnapshot,
    *,
    default_region: str,
) -> _UpdateFieldPlan:
    """Build an all-or-nothing fill-blank plan for selected provider fields."""
    email_patch: str | None = None
    phone_patch: str | None = None

    if request.trusted_verified_email is not None:
        existing_email = _normalize_email(provider.email)
        if existing_email is _INVALID_EMAIL:
            return _UpdateFieldPlan(
                conflict=True,
                error_code=CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
            )
        if existing_email is None:
            email_patch = request.trusted_verified_email
        elif existing_email != request.trusted_verified_email:
            return _UpdateFieldPlan(
                conflict=True,
                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
            )

    if request.trusted_verified_phone is not None:
        existing_phone = _normalize_phone(provider.phone, default_region)
        if existing_phone is _INVALID_PHONE:
            return _UpdateFieldPlan(
                conflict=True,
                error_code=CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
            )
        if existing_phone is None:
            phone_patch = request.trusted_verified_phone
        elif existing_phone != request.trusted_verified_phone:
            return _UpdateFieldPlan(
                conflict=True,
                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
            )

    return _UpdateFieldPlan(patch_email=email_patch, patch_phone=phone_patch)


class _InvalidEvidence:
    pass


_INVALID_EMAIL = _InvalidEvidence()
_INVALID_PHONE = _InvalidEvidence()


def _normalize_email(value: object) -> str | _InvalidEvidence | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_EMAIL
    normalized = value.strip().casefold()
    if not normalized:
        return None
    if (
        len(normalized) > 254
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        return _INVALID_EMAIL
    return normalized


def _normalize_phone(value: object, default_region: str) -> str | _InvalidEvidence | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        return _INVALID_PHONE
    if not value.strip():
        return None
    try:
        return normalize_phone_number(value, default_region)
    except PhoneNormalizationError:
        return _INVALID_PHONE


async def _current_link(
    session_factory: async_sessionmaker[AsyncSession], snapshot: _UpdateSnapshot
) -> bool:
    async with session_factory() as session:
        link = await session.get(CRMContactLink, snapshot.contact_id)
    return bool(
        snapshot.link_state is CRMContactLinkState.ACTIVE
        and link is not None
        and link.state is CRMContactLinkState.ACTIVE
        and link.hubspot_portal_id == snapshot.portal_id
        and link.hubspot_contact_id == snapshot.hubspot_contact_id
        and link.linked_by is snapshot.linked_by
    )


async def _local_state_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    settings: Settings,
) -> bool:
    if not _settings_unchanged(settings, snapshot):
        return False
    async with session_factory() as session:
        contact = await session.get(Contact, snapshot.contact_id)
        conversation = await session.get(Conversation, snapshot.conversation_id)
        business = await session.get(BusinessConfig, 1)
        link = await session.get(CRMContactLink, snapshot.contact_id)
    if (
        contact is None
        or conversation is None
        or conversation.contact_id != snapshot.contact_id
        or contact.phone_e164 != snapshot.phone_e164
        or business is None
        or business.default_phone_region != snapshot.default_phone_region
        or link is None
        or link.state is not CRMContactLinkState.ACTIVE
        or link.hubspot_portal_id != snapshot.portal_id
        or link.hubspot_contact_id != snapshot.hubspot_contact_id
        or link.linked_by is not snapshot.linked_by
    ):
        return False
    if contact.email == snapshot.local_email:
        return True
    return (
        snapshot.local_email is None
        and snapshot.selected_email is not None
        and _normalize_email(contact.email) == snapshot.selected_email
    )


async def _mark_stale(
    session_factory: async_sessionmaker[AsyncSession], snapshot: _UpdateSnapshot
) -> None:
    try:
        async with session_factory.begin() as session:
            link = await session.scalar(
                select(CRMContactLink)
                .where(CRMContactLink.contact_id == snapshot.contact_id)
                .with_for_update()
            )
            if (
                link is not None
                and link.state is CRMContactLinkState.ACTIVE
                and link.hubspot_portal_id == snapshot.portal_id
                and link.hubspot_contact_id == snapshot.hubspot_contact_id
                and link.linked_by is snapshot.linked_by
            ):
                link.state = CRMContactLinkState.STALE
    except SQLAlchemyError:
        return


async def _persist_success(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    request: CRMContactUpdateRequest,
    *,
    updated_fields: tuple[Literal["email", "phone"], ...] = (),
) -> CRMContactUpdateResult:
    result = _result(
        CRMContactUpdateOutcome.UPDATED if updated_fields else CRMContactUpdateOutcome.UNCHANGED,
        replayed=snapshot.replayed,
        updated_fields=updated_fields,
    )
    try:
        async with session_factory.begin() as session:
            contact = await session.scalar(
                select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
            )
            conversation = await session.get(Conversation, snapshot.conversation_id)
            link = await session.scalar(
                select(CRMContactLink)
                .where(CRMContactLink.contact_id == snapshot.contact_id)
                .with_for_update()
            )
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            business = await session.get(BusinessConfig, 1)
            if (
                contact is None
                or conversation is None
                or conversation.contact_id != snapshot.contact_id
                or contact.phone_e164 != snapshot.phone_e164
                or business is None
                or business.default_phone_region != snapshot.default_phone_region
                or link is None
                or link.state is not CRMContactLinkState.ACTIVE
                or link.hubspot_portal_id != snapshot.portal_id
                or link.hubspot_contact_id != snapshot.hubspot_contact_id
                or link.linked_by is not snapshot.linked_by
                or execution is None
                or execution.status is not ToolExecutionStatus.STARTED
            ):
                raise SQLAlchemyError("local state changed")
            local_email = _normalize_email(contact.email)
            if request.trusted_verified_email is not None:
                if contact.email is None or not contact.email.strip():
                    contact.email = request.trusted_verified_email
                elif local_email != request.trusted_verified_email:
                    raise SQLAlchemyError("local email changed")
            elif contact.email != snapshot.local_email:
                raise SQLAlchemyError("local email changed")
            link.verified_at = datetime.now(UTC)
            _terminalize(session, execution, snapshot, result)
        return result
    except (IntegrityError, SQLAlchemyError):
        return await _record_uncertain(
            session_factory,
            snapshot,
            _result(
                CRMContactUpdateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )


async def _finish_or_retain(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    result: CRMContactUpdateResult,
) -> CRMContactUpdateResult:
    if not snapshot.recovery:
        return await _finish(session_factory, snapshot, result)
    return await _record_uncertain(
        session_factory,
        snapshot,
        _result(
            CRMContactUpdateOutcome.UNCERTAIN,
            replayed=True,
            error_code=result.error_code or CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE,
            next_action=(
                CRMContactWriteNextAction.RECONCILE_SAME_KEY
                if result.next_action
                in {
                    CRMContactWriteNextAction.NONE,
                    CRMContactWriteNextAction.RETRY_SAME_KEY,
                }
                else result.next_action
            ),
        ),
    )


async def _identity_failure(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    result: CRMContactUpdateResult | None = None,
) -> CRMContactUpdateResult:
    conflict = result or _conflict_result(snapshot, CRMContactWriteErrorCode.IDENTITY_CONFLICT)
    if snapshot.recovery:
        return await _finish_or_retain(session_factory, snapshot, conflict)
    return await _finish(session_factory, snapshot, conflict)


async def _provider_failure(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    error: HubSpotProviderError,
    *,
    before_write: bool,
) -> CRMContactUpdateResult:
    uncertain = error.outcome_uncertain or snapshot.recovery
    result = _provider_failure_result(error, replayed=snapshot.replayed, uncertain=uncertain)
    if uncertain:
        return await _record_uncertain(session_factory, snapshot, result)
    # Read/preflight failures prove that no PATCH was sent; only the explicit
    # same-key path may retry the narrow provider-unavailable/rate-limit cases.
    del before_write
    return await _finish(session_factory, snapshot, result)


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    result: CRMContactUpdateResult,
) -> CRMContactUpdateResult:
    if result.outcome is CRMContactUpdateOutcome.UNCERTAIN:
        return await _record_uncertain(session_factory, snapshot, result)
    try:
        async with session_factory.begin() as session:
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            if execution is None:
                return _result(
                    CRMContactUpdateOutcome.UNCERTAIN,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                    next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                )
            if execution.status is not ToolExecutionStatus.STARTED:
                return _stored_replay(execution)
            _terminalize(session, execution, snapshot, result)
        return result
    except SQLAlchemyError:
        return await _record_uncertain(
            session_factory,
            snapshot,
            _result(
                CRMContactUpdateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )


async def _record_uncertain(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _UpdateSnapshot,
    result: CRMContactUpdateResult,
) -> CRMContactUpdateResult:
    try:
        async with session_factory.begin() as session:
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            if execution is None:
                return result
            if execution.status is not ToolExecutionStatus.STARTED:
                return _stored_replay(execution)
            execution.status = ToolExecutionStatus.STARTED
            execution.sanitized_result = result.model_dump(mode="json")
            execution.safe_error_code = (
                None if result.error_code is None else result.error_code.value
            )
            execution.safe_error_message = "CRM Contact update requires same-key reconciliation"
            execution.finished_at = None
            session.add(_audit(snapshot, "uncertain"))
    except SQLAlchemyError:
        return result
    return result


def _terminalize(
    session: AsyncSession,
    execution: ToolExecution,
    snapshot: _UpdateSnapshot,
    result: CRMContactUpdateResult,
) -> None:
    if result.outcome in {
        CRMContactUpdateOutcome.UPDATED,
        CRMContactUpdateOutcome.UNCHANGED,
    }:
        execution.status = ToolExecutionStatus.SUCCEEDED
    elif result.outcome in {
        CRMContactUpdateOutcome.NOT_CONFIGURED,
        CRMContactUpdateOutcome.INVALID_REQUEST,
        CRMContactUpdateOutcome.OWNERSHIP_REJECTED,
        CRMContactUpdateOutcome.CONFLICT,
    }:
        execution.status = ToolExecutionStatus.REJECTED
    else:
        execution.status = ToolExecutionStatus.FAILED
    execution.sanitized_result = result.model_dump(mode="json")
    execution.safe_error_code = None if result.error_code is None else result.error_code.value
    execution.safe_error_message = (
        None if result.error_code is None else "CRM Contact update did not complete"
    )
    execution.finished_at = datetime.now(UTC)
    session.add(_audit(snapshot, result.outcome.value))


def _audit(snapshot: _UpdateSnapshot, outcome: str) -> AuditEvent:
    return AuditEvent(
        event_type=_OPERATION,
        actor_type=AuditActorType.SYSTEM,
        contact_id=snapshot.contact_id,
        conversation_id=snapshot.conversation_id,
        entity_type="hubspot_contact",
        entity_identifier=snapshot.hubspot_contact_id,
        sanitized_metadata={
            "operation": _OPERATION,
            "outcome": outcome,
            "tool_execution_id": str(snapshot.execution_id),
            "replayed": snapshot.replayed,
        },
    )


def _result(
    outcome: CRMContactUpdateOutcome,
    *,
    replayed: bool = False,
    updated_fields: tuple[Literal["email", "phone"], ...] = (),
    error_code: CRMContactWriteErrorCode | None = None,
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE,
) -> CRMContactUpdateResult:
    return CRMContactUpdateResult(
        outcome=outcome,
        replayed=replayed,
        updated_fields=updated_fields,
        error_code=error_code,
        next_action=next_action,
    )


def _stored_replay(execution: ToolExecution) -> CRMContactUpdateResult:
    if isinstance(execution.sanitized_result, dict):
        try:
            stored = CRMContactUpdateResult.model_validate(execution.sanitized_result)
        except ValueError:
            stored = None
        if stored is not None:
            return stored.model_copy(update={"replayed": True})
    return _result(
        CRMContactUpdateOutcome.UNCERTAIN,
        replayed=True,
        error_code=CRMContactWriteErrorCode.OPERATION_IN_PROGRESS,
        next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
    )


def _retryable_failure(execution: ToolExecution) -> bool:
    if execution.status is not ToolExecutionStatus.FAILED:
        return False
    if not isinstance(execution.sanitized_result, dict):
        return False
    try:
        result = CRMContactUpdateResult.model_validate(execution.sanitized_result)
    except ValueError:
        return False
    return (
        result.outcome is CRMContactUpdateOutcome.FAILED
        and result.next_action is CRMContactWriteNextAction.RETRY_SAME_KEY
        and result.error_code
        in {
            CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE,
            CRMContactWriteErrorCode.RATE_LIMITED,
        }
    )


def _provider_failure_result(
    error: HubSpotProviderError,
    *,
    replayed: bool,
    uncertain: bool,
) -> CRMContactUpdateResult:
    if error.code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}:
        return _result(
            CRMContactUpdateOutcome.UNCERTAIN if uncertain else CRMContactUpdateOutcome.FAILED,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.PERMISSION_DENIED,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if error.code is HubSpotErrorCode.VALIDATION:
        return _result(
            CRMContactUpdateOutcome.UNCERTAIN if uncertain else CRMContactUpdateOutcome.FAILED,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.PROVIDER_VALIDATION,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if error.code is HubSpotErrorCode.RATE_LIMITED:
        return _result(
            CRMContactUpdateOutcome.UNCERTAIN if uncertain else CRMContactUpdateOutcome.FAILED,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.RATE_LIMITED,
            next_action=(
                CRMContactWriteNextAction.RECONCILE_SAME_KEY
                if uncertain
                else CRMContactWriteNextAction.RETRY_SAME_KEY
            ),
        )
    if error.code is HubSpotErrorCode.CONFLICT:
        return _result(
            CRMContactUpdateOutcome.UNCERTAIN if uncertain else CRMContactUpdateOutcome.CONFLICT,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
            next_action=(
                CRMContactWriteNextAction.RECONCILE_SAME_KEY
                if uncertain
                else CRMContactWriteNextAction.OPERATOR_REVIEW
            ),
        )
    if error.code is HubSpotErrorCode.NOT_CONFIGURED:
        outcome = (
            CRMContactUpdateOutcome.UNCERTAIN
            if uncertain
            else CRMContactUpdateOutcome.NOT_CONFIGURED
        )
        return _result(
            outcome,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    code = (
        CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
        if error.code in {HubSpotErrorCode.INVALID_RESPONSE, HubSpotErrorCode.RESPONSE_TOO_LARGE}
        else CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
    )
    retry = error.code in {
        HubSpotErrorCode.TIMEOUT,
        HubSpotErrorCode.PROVIDER_UNAVAILABLE,
        HubSpotErrorCode.RATE_LIMITED,
    }
    return _result(
        CRMContactUpdateOutcome.UNCERTAIN if uncertain else CRMContactUpdateOutcome.FAILED,
        replayed=replayed,
        error_code=code,
        next_action=(
            CRMContactWriteNextAction.RECONCILE_SAME_KEY
            if uncertain
            else CRMContactWriteNextAction.RETRY_SAME_KEY
            if retry
            else CRMContactWriteNextAction.OPERATOR_REVIEW
        ),
    )


def _preflight_failure(
    preflight: HubSpotPreflightResult, *, replayed: bool
) -> CRMContactUpdateResult | None:
    if (
        preflight.outcome is HubSpotPreflightOutcome.VERIFIED
        and preflight.account_identity_verified
    ):
        return None
    if preflight.outcome is HubSpotPreflightOutcome.PERMISSION_DENIED:
        code = CRMContactWriteErrorCode.PERMISSION_DENIED
    elif preflight.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH:
        code = CRMContactWriteErrorCode.ACCOUNT_MISMATCH
    elif preflight.outcome is HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE:
        code = CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
    elif preflight.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE:
        code = CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
    else:
        code = CRMContactWriteErrorCode.NOT_CONFIGURED
    retry = code is CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
    return _result(
        CRMContactUpdateOutcome.FAILED
        if retry
        or code
        in {
            CRMContactWriteErrorCode.PERMISSION_DENIED,
            CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
        }
        else CRMContactUpdateOutcome.NOT_CONFIGURED,
        replayed=replayed,
        error_code=code,
        next_action=(
            CRMContactWriteNextAction.RETRY_SAME_KEY
            if retry
            else CRMContactWriteNextAction.OPERATOR_REVIEW
        ),
    )


def _validation_failure(
    validation: CRMContactWriteValidationResult,
) -> CRMContactUpdateResult:
    if validation.outcome is CRMContactWriteValidationOutcome.OWNERSHIP_MISMATCH:
        outcome = CRMContactUpdateOutcome.OWNERSHIP_REJECTED
    elif validation.outcome is CRMContactWriteValidationOutcome.IDENTITY_CONFLICT:
        outcome = CRMContactUpdateOutcome.CONFLICT
    elif validation.outcome is CRMContactWriteValidationOutcome.FAILED:
        outcome = CRMContactUpdateOutcome.FAILED
    else:
        outcome = CRMContactUpdateOutcome.INVALID_REQUEST
    return _result(
        outcome,
        error_code=validation.error_code,
        next_action=validation.next_action,
    )


def _conflict_result(
    snapshot: _UpdateSnapshot, code: CRMContactWriteErrorCode
) -> CRMContactUpdateResult:
    return _result(
        CRMContactUpdateOutcome.CONFLICT,
        replayed=snapshot.replayed,
        error_code=code,
        next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
    )


def _selected_values_equal(
    request: CRMContactUpdateRequest,
    provider: HubSpotContactSnapshot,
    default_region: str,
) -> bool:
    return not (
        request.trusted_verified_email is not None
        and _normalize_email(provider.email) != request.trusted_verified_email
    ) and not (
        request.trusted_verified_phone is not None
        and _normalize_phone(provider.phone, default_region) != request.trusted_verified_phone
    )


def _plan_field_names(
    plan: _UpdateFieldPlan,
) -> tuple[Literal["email", "phone"], ...]:
    names: list[Literal["email", "phone"]] = []
    if plan.patch_email is not None:
        names.append("email")
    if plan.patch_phone is not None:
        names.append("phone")
    return tuple(names)


def _selected_field_names(
    request: CRMContactUpdateRequest,
) -> tuple[Literal["email", "phone"], ...]:
    names: list[Literal["email", "phone"]] = []
    if request.trusted_verified_email is not None:
        names.append("email")
    if request.trusted_verified_phone is not None:
        names.append("phone")
    return tuple(names)


def _evidence_tags(
    request: CRMContactUpdateRequest,
) -> tuple[str | None, str | None]:
    key = request.idempotency_key.encode("ascii")

    def tag(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold() if "@" in value else value.strip()
        if not normalized:
            raise ValueError("trusted value is blank")
        return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    return tag(request.trusted_verified_email), tag(request.trusted_verified_phone)


def _durable_key(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _settings_configured(settings: Settings) -> bool:
    key = settings.hubspot_service_key
    portal = settings.hubspot_portal_id
    return bool(
        key is not None
        and key.get_secret_value().strip()
        and portal is not None
        and portal.isascii()
        and portal.isdecimal()
        and int(portal) > 0
    )


def _settings_unchanged(settings: Settings, snapshot: _UpdateSnapshot) -> bool:
    return (
        settings.hubspot_service_key is snapshot.bound_key
        and settings.hubspot_portal_id == snapshot.portal_id
        and _settings_configured(settings)
    )

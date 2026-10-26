"""Explicit, recoverable HubSpot Contact creation application operation.

The request's trusted values do not establish caller authority. Until a real
trusted caller and live write capabilities exist, production writes remain
disabled by HubSpotClient; only its isolated test/CI MockTransport path can
exercise this operation.
"""

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

from receptionist.application.booking import (
    ToolExecutionClaim,
    ToolExecutionClaimOutcome,
    _claim_tool_execution,
)
from receptionist.application.crm_contact_writes import (
    CRMContactCreateOutcome,
    CRMContactCreateRequest,
    CRMContactCreateResult,
    CRMContactWriteErrorCode,
    CRMContactWriteNextAction,
    CRMContactWriteValidationOutcome,
    CRMContactWriteValidationResult,
    contact_identity_conflicts,
    validate_crm_contact_write,
)
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
from receptionist.domain.identity import CRMContactLinkProvenance
from receptionist.integrations.hubspot import (
    HubSpotClient,
    HubSpotContactSearchResult,
    HubSpotContactSnapshot,
    HubSpotErrorCode,
    HubSpotPreflightOutcome,
    HubSpotPreflightResult,
    HubSpotProviderError,
)

_OPERATION: Literal["create_crm_contact"] = "create_crm_contact"
_CRM_MUTATIONS = ("create_crm_contact", "update_crm_contact", "record_booking_outcome")
_EMAIL_LIMIT = 254


@dataclass(frozen=True, slots=True)
class _CreateSnapshot:
    contact_id: UUID
    conversation_id: UUID
    phone_e164: str | None
    local_email: str | None = field(repr=False)
    trusted_email: str = field(repr=False)
    trusted_phone: str | None = field(repr=False)
    default_phone_region: str
    portal_id: str
    execution_id: UUID
    replayed: bool
    recovery: bool

    @property
    def identity_phone(self) -> str | None:
        return self.trusted_phone or self.phone_e164


@dataclass(frozen=True, slots=True)
class _IdentityLookup:
    decision: CRMContactIdentityDecision
    matched_contact: HubSpotContactSnapshot | None


async def create_crm_contact(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactCreateRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CRMContactCreateResult:
    """Create one Contact after durable claim, verified account binding and no-match reads.

    This internal operation does not authenticate its caller. The caller must
    have independently established trusted provenance for the supplied values.
    """
    try:
        email_tag, phone_tag = _trusted_evidence_tags(
            request.idempotency_key,
            email=request.trusted_verified_email,
            phone=request.trusted_verified_phone,
        )
    except (UnicodeEncodeError, ValueError):
        return _create_result(
            CRMContactCreateOutcome.INVALID_REQUEST,
            error_code=CRMContactWriteErrorCode.INVALID_REQUEST,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )

    validation = await validate_crm_contact_write(session_factory, request)
    if validation.outcome is not CRMContactWriteValidationOutcome.VALID:
        return _validation_failure(validation)

    try:
        claimed = await _claim_create_operation(
            session_factory,
            request,
            email_tag=email_tag,
            phone_tag=phone_tag,
            settings=settings,
        )
    except SQLAlchemyError:
        return _create_result(
            CRMContactCreateOutcome.FAILED,
            error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if isinstance(claimed, CRMContactCreateResult):
        return claimed
    return await _run_claimed_create(
        session_factory,
        claimed,
        request,
        settings,
        transport=transport,
    )


def _trusted_evidence_tags(
    idempotency_key: str,
    *,
    email: str | None,
    phone: str | None,
) -> tuple[str | None, str | None]:
    """Bind normalized PII to a claim without persisting PII or the caller key."""
    key = idempotency_key.encode("ascii")

    def tag(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold() if "@" in value else value.strip()
        if not normalized:
            raise ValueError("trusted identity value is blank")
        return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    return tag(email), tag(phone)


async def _claim_create_operation(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactCreateRequest,
    *,
    email_tag: str | None,
    phone_tag: str | None,
    settings: Settings,
) -> _CreateSnapshot | CRMContactCreateResult:
    durable_key = hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
    async with session_factory.begin() as session:
        contact = await session.scalar(
            select(Contact).where(Contact.id == request.contact_id).with_for_update()
        )
        conversation = await session.get(Conversation, request.conversation_id)
        if contact is None or conversation is None:
            return _create_result(
                CRMContactCreateOutcome.INVALID_REQUEST,
                error_code=CRMContactWriteErrorCode.INVALID_REQUEST,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        if conversation.contact_id != contact.id:
            return _create_result(
                CRMContactCreateOutcome.OWNERSHIP_REJECTED,
                error_code=CRMContactWriteErrorCode.OWNERSHIP_MISMATCH,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        if contact_identity_conflicts(contact, request):
            return _create_result(
                CRMContactCreateOutcome.IDENTITY_CONFLICT,
                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        arguments: dict[str, object] = {
            "conversation_id": str(request.conversation_id),
            "contact_id": str(request.contact_id),
            "hubspot_portal_id": settings.hubspot_portal_id,
            "trusted_email_supplied": True,
            "trusted_email_identity_tag": email_tag,
            "trusted_phone_supplied": request.trusted_verified_phone is not None,
            "trusted_phone_identity_tag": phone_tag,
        }
        existing = await session.scalar(
            select(ToolExecution)
            .where(ToolExecution.idempotency_key == durable_key)
            .with_for_update()
        )
        if existing is not None:
            claim = await _claim_tool_execution(
                session,
                conversation_id=request.conversation_id,
                tool_name=_OPERATION,
                idempotency_key=durable_key,
                sanitized_arguments=arguments,
            )
            if claim.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT:
                return _create_result(
                    CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.IDEMPOTENCY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            retryable_failure = (
                claim.outcome is ToolExecutionClaimOutcome.FAILED_REPLAY
                and _retryable_no_write_failure(claim.execution)
            )
            if (
                claim.outcome is not ToolExecutionClaimOutcome.STARTED_RECOVERY
                and not retryable_failure
            ):
                return _stored_replay(claim.execution)
            business = await session.get(BusinessConfig, 1)
            if business is None:
                return _create_result(
                    CRMContactCreateOutcome.UNCERTAIN
                    if claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY
                    else CRMContactCreateOutcome.NOT_CONFIGURED,
                    replayed=claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY,
                    error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            if await session.get(CRMContactLink, contact.id) is not None:
                return _create_result(
                    CRMContactCreateOutcome.UNCERTAIN
                    if claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY
                    else CRMContactCreateOutcome.IDENTITY_CONFLICT,
                    replayed=claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            if retryable_failure:
                claim.execution.status = ToolExecutionStatus.STARTED
                claim.execution.sanitized_result = None
                claim.execution.safe_error_code = None
                claim.execution.safe_error_message = None
                claim.execution.finished_at = None
                snapshot = _snapshot(
                    contact, conversation, business, request, settings, claim, recovery=False
                )
                session.add(_audit_event(snapshot, "retry_started", replayed=True))
                return snapshot
            if claim.outcome is not ToolExecutionClaimOutcome.STARTED_RECOVERY:
                return _stored_replay(claim.execution)
            return _snapshot(
                contact, conversation, business, request, settings, claim, recovery=True
            )

        business = await session.get(BusinessConfig, 1)
        if business is None:
            return _create_result(
                CRMContactCreateOutcome.NOT_CONFIGURED,
                error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        if not _settings_structurally_configured(settings):
            return _create_result(
                CRMContactCreateOutcome.NOT_CONFIGURED,
                error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        if await session.get(CRMContactLink, contact.id) is not None:
            return _create_result(
                CRMContactCreateOutcome.IDENTITY_CONFLICT,
                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )

        contact_conversations = select(Conversation.id).where(Conversation.contact_id == contact.id)
        active = await session.scalar(
            select(ToolExecution.id)
            .where(
                ToolExecution.conversation_id.in_(contact_conversations),
                ToolExecution.tool_name.in_(_CRM_MUTATIONS),
                ToolExecution.status == ToolExecutionStatus.STARTED,
                ToolExecution.idempotency_key != durable_key,
            )
            .limit(1)
        )
        if active is not None:
            return _create_result(
                CRMContactCreateOutcome.OPERATION_IN_PROGRESS,
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
            return _create_result(
                CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT,
                error_code=CRMContactWriteErrorCode.IDEMPOTENCY_CONFLICT,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            )
        if claim.outcome is not ToolExecutionClaimOutcome.NEW:
            if claim.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY:
                return _snapshot(
                    contact, conversation, business, request, settings, claim, recovery=True
                )
            return _stored_replay(claim.execution)

        snapshot = _snapshot(
            contact, conversation, business, request, settings, claim, recovery=False
        )
        session.add(_audit_event(snapshot, "started", replayed=False))
        return snapshot


def _snapshot(
    contact: Contact,
    conversation: Conversation,
    business: BusinessConfig,
    request: CRMContactCreateRequest,
    settings: Settings,
    claim: ToolExecutionClaim,
    *,
    recovery: bool,
) -> _CreateSnapshot:
    return _CreateSnapshot(
        contact_id=contact.id,
        conversation_id=conversation.id,
        phone_e164=contact.phone_e164,
        local_email=contact.email,
        trusted_email=request.trusted_verified_email,
        trusted_phone=request.trusted_verified_phone,
        default_phone_region=business.default_phone_region,
        portal_id=settings.hubspot_portal_id or "",
        execution_id=claim.execution.id,
        replayed=recovery,
        recovery=recovery,
    )


async def _run_claimed_create(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    request: CRMContactCreateRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> CRMContactCreateResult:
    if (
        not _settings_structurally_configured(settings)
        or settings.hubspot_portal_id != snapshot.portal_id
    ):
        return await _finish_prewrite_result(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.NOT_CONFIGURED,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
            ),
        )
    bound_key = settings.hubspot_service_key
    bound_portal = settings.hubspot_portal_id
    try:
        preflight_client = HubSpotClient(settings, transport=transport)
    except HubSpotProviderError as error:
        return await _complete_provider_failure(session_factory, snapshot, error)

    operation_client: HubSpotClient | None = None
    try:
        preflight = await preflight_client.preflight_account()
        failure = _preflight_failure(preflight)
        if failure is not None:
            return await _finish_prewrite_result(session_factory, snapshot, failure)
        if not _configuration_unchanged(settings, bound_key, bound_portal):
            return await _finish_prewrite_result(
                session_factory,
                snapshot,
                _create_result(
                    CRMContactCreateOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        test_mock_only = settings.app_env in {"test", "ci"} and isinstance(
            transport, httpx.MockTransport
        )
        if preflight.writes_enabled:
            operation_client = preflight_client
        elif test_mock_only:
            # The client under test must remain preflight-free for its existing
            # isolated MockTransport guard. This never changes capability state.
            operation_client = HubSpotClient(settings, transport=transport)
        else:
            return await _complete_result(
                session_factory,
                snapshot,
                _create_result(
                    CRMContactCreateOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.ACTIVATION_REQUIRED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        lookup = await _lookup_identity(operation_client, snapshot)
        if lookup.decision.outcome is CRMContactIdentityOutcome.MATCHED:
            if snapshot.recovery:
                return await _persist_reconciled_link(
                    session_factory,
                    snapshot,
                    lookup.matched_contact,
                )
            return await _complete_result(
                session_factory,
                snapshot,
                _create_result(
                    CRMContactCreateOutcome.EXISTING_CONTACT,
                    provider_id=lookup.decision.hubspot_contact_id,
                    next_action=CRMContactWriteNextAction.NONE,
                ),
            )

        if lookup.decision.outcome is not CRMContactIdentityOutcome.NOT_FOUND:
            code = (
                CRMContactWriteErrorCode.IDENTITY_CONFLICT
                if lookup.decision.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT
                else CRMContactWriteErrorCode.AMBIGUOUS_IDENTITY
            )
            if snapshot.recovery:
                return await _record_uncertain(
                    session_factory,
                    snapshot,
                    _create_result(
                        CRMContactCreateOutcome.UNCERTAIN,
                        replayed=True,
                        error_code=code,
                        next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                    ),
                )
            return await _complete_result(
                session_factory,
                snapshot,
                _create_result(
                    CRMContactCreateOutcome.IDENTITY_CONFLICT,
                    error_code=code,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                ),
            )

        if snapshot.recovery:
            return await _record_uncertain(
                session_factory,
                snapshot,
                _create_result(
                    CRMContactCreateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE,
                    next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                ),
            )
        return await _create_after_no_match(
            session_factory,
            snapshot,
            operation_client,
            settings,
            bound_key=bound_key,
            bound_portal=bound_portal,
        )
    except HubSpotProviderError as error:
        return await _complete_provider_failure(session_factory, snapshot, error)
    except SQLAlchemyError:
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )
    finally:
        if operation_client is not None and operation_client is not preflight_client:
            await operation_client.aclose()
        await preflight_client.aclose()


async def _lookup_identity(
    client: HubSpotClient,
    snapshot: _CreateSnapshot,
) -> _IdentityLookup:
    phone_search: HubSpotContactSearchResult | None = None
    if snapshot.identity_phone is not None:
        phone_search = await client.search_contacts_by_phone(snapshot.identity_phone)

    email_match: HubSpotContactSnapshot | None = None
    try:
        email_match = await client.get_contact_by_email(snapshot.trusted_email)
    except HubSpotProviderError as error:
        if error.code is not HubSpotErrorCode.NOT_FOUND:
            raise

    decision = decide_crm_contact_identity(
        local_phone_e164=snapshot.identity_phone,
        trusted_verified_email=snapshot.trusted_email,
        phone_search=phone_search,
        email_match=email_match,
        email_lookup_complete=True,
        default_region=snapshot.default_phone_region,
    )
    matched = None
    if decision.hubspot_contact_id is not None:
        if email_match is not None and email_match.id == decision.hubspot_contact_id:
            matched = email_match
        elif phone_search is not None:
            matched = next(
                (item for item in phone_search.results if item.id == decision.hubspot_contact_id),
                None,
            )
    return _IdentityLookup(decision=decision, matched_contact=matched)


async def _create_after_no_match(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    client: HubSpotClient,
    settings: Settings,
    *,
    bound_key: SecretStr | None,
    bound_portal: str | None,
) -> CRMContactCreateResult:
    try:
        async with session_factory.begin() as session:
            contact = await session.scalar(
                select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
            )
            conversation = await session.get(Conversation, snapshot.conversation_id)
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            business = await session.get(BusinessConfig, 1)
            if execution is None or execution.status is not ToolExecutionStatus.STARTED:
                return (
                    _stored_replay(execution)
                    if execution is not None
                    else _create_result(
                        CRMContactCreateOutcome.UNCERTAIN,
                        replayed=True,
                        error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                        next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                    )
                )
            if (
                contact is None
                or conversation is None
                or conversation.contact_id != snapshot.contact_id
                or contact.phone_e164 != snapshot.phone_e164
                or _normalize_email(contact.email) != _normalize_email(snapshot.local_email)
                or business is None
                or business.default_phone_region != snapshot.default_phone_region
            ):
                result = _create_result(
                    CRMContactCreateOutcome.IDENTITY_CONFLICT,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                _terminalize(session, snapshot, execution, result)
                return result
            link = await session.get(CRMContactLink, snapshot.contact_id)
            if link is not None:
                result = _create_result(
                    CRMContactCreateOutcome.IDENTITY_CONFLICT,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                _terminalize(session, snapshot, execution, result)
                return result
            if not _configuration_unchanged(settings, bound_key, bound_portal):
                result = _create_result(
                    CRMContactCreateOutcome.NOT_CONFIGURED,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                _terminalize(session, snapshot, execution, result)
                return result

            try:
                provider_contact = await client.create_contact(
                    email=snapshot.trusted_email,
                    phone=snapshot.trusted_phone,
                )
            except HubSpotProviderError as error:
                if error.outcome_uncertain:
                    result = _provider_error_result(error, uncertain=True, replayed=False)
                    _store_uncertain(session, snapshot, execution, result)
                    return result
                if error.code is HubSpotErrorCode.CONFLICT:
                    try:
                        conflict_lookup = await _lookup_identity(client, snapshot)
                    except HubSpotProviderError as read_error:
                        result = _provider_error_result(read_error, uncertain=True, replayed=False)
                        _store_uncertain(session, snapshot, execution, result)
                        return result
                    else:
                        if conflict_lookup.decision.outcome is CRMContactIdentityOutcome.MATCHED:
                            result = _create_result(
                                CRMContactCreateOutcome.EXISTING_CONTACT,
                                provider_id=conflict_lookup.decision.hubspot_contact_id,
                                error_code=CRMContactWriteErrorCode.CONTACT_ALREADY_EXISTS,
                            )
                        else:
                            result = _create_result(
                                CRMContactCreateOutcome.IDENTITY_CONFLICT,
                                error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                                next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                            )
                    _terminalize(session, snapshot, execution, result)
                    return result
                result = _provider_error_result(error, uncertain=False, replayed=False)
                _terminalize(session, snapshot, execution, result)
                return result

            verified = _verify_created_identity(snapshot, provider_contact)
            if verified.outcome is not CRMContactIdentityOutcome.MATCHED:
                result = _create_result(
                    CRMContactCreateOutcome.UNCERTAIN,
                    replayed=False,
                    error_code=(
                        CRMContactWriteErrorCode.IDENTITY_CONFLICT
                        if verified.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT
                        else CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
                    ),
                    next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                )
                _store_uncertain(session, snapshot, execution, result)
                return result
            if provider_contact.archived or verified.linked_by is None:
                result = _create_result(
                    CRMContactCreateOutcome.UNCERTAIN,
                    error_code=CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
                    next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                )
                _store_uncertain(session, snapshot, execution, result)
                return result

            result = _create_result(
                CRMContactCreateOutcome.CREATED,
                provider_id=provider_contact.id,
            )
            _persist_local_link(session, contact, snapshot, provider_contact, verified.linked_by)
            await session.flush()
            _terminalize(session, snapshot, execution, result)
            return result
    except (IntegrityError, SQLAlchemyError):
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )


def _verify_created_identity(
    snapshot: _CreateSnapshot,
    provider_contact: HubSpotContactSnapshot,
) -> CRMContactIdentityDecision:
    phone_search = (
        None
        if snapshot.identity_phone is None
        else HubSpotContactSearchResult(results=(), has_more=False, next_after=None)
    )
    decision = decide_crm_contact_identity(
        local_phone_e164=snapshot.identity_phone,
        trusted_verified_email=snapshot.trusted_email,
        phone_search=phone_search,
        email_match=provider_contact,
        email_lookup_complete=True,
        default_region=snapshot.default_phone_region,
    )
    if decision.outcome is not CRMContactIdentityOutcome.MATCHED:
        return decision
    if _normalize_email(provider_contact.email) != snapshot.trusted_email:
        return CRMContactIdentityDecision(outcome=CRMContactIdentityOutcome.AMBIGUOUS)
    return CRMContactIdentityDecision(
        outcome=CRMContactIdentityOutcome.MATCHED,
        hubspot_contact_id=decision.hubspot_contact_id,
        linked_by=CRMContactLinkProvenance.VERIFIED_EMAIL,
    )


def _persist_local_link(
    session: AsyncSession,
    contact: Contact,
    snapshot: _CreateSnapshot,
    provider_contact: HubSpotContactSnapshot,
    provenance: CRMContactLinkProvenance,
) -> None:
    if contact.email is None or not contact.email.strip():
        contact.email = snapshot.trusted_email
    session.add(
        CRMContactLink(
            contact_id=snapshot.contact_id,
            hubspot_portal_id=snapshot.portal_id,
            hubspot_contact_id=provider_contact.id,
            linked_by=provenance,
            state=CRMContactLinkState.ACTIVE,
            verified_at=datetime.now(UTC),
        )
    )


async def _persist_reconciled_link(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    provider_contact: HubSpotContactSnapshot | None,
) -> CRMContactCreateResult:
    if provider_contact is None or provider_contact.archived:
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=True,
                error_code=CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )
    verified = _verify_created_identity(snapshot, provider_contact)
    if verified.outcome is not CRMContactIdentityOutcome.MATCHED or verified.linked_by is None:
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=True,
                error_code=(
                    CRMContactWriteErrorCode.IDENTITY_CONFLICT
                    if verified.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT
                    else CRMContactWriteErrorCode.AMBIGUOUS_IDENTITY
                ),
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )

    try:
        async with session_factory.begin() as session:
            contact = await session.scalar(
                select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
            )
            conversation = await session.get(Conversation, snapshot.conversation_id)
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            business = await session.get(BusinessConfig, 1)
            if execution is None or execution.status is not ToolExecutionStatus.STARTED:
                return (
                    _stored_replay(execution)
                    if execution is not None
                    else _create_result(
                        CRMContactCreateOutcome.UNCERTAIN,
                        replayed=True,
                        error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                        next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
                    )
                )
            if (
                contact is None
                or conversation is None
                or conversation.contact_id != snapshot.contact_id
                or contact.phone_e164 != snapshot.phone_e164
                or _normalize_email(contact.email) != _normalize_email(snapshot.local_email)
                or business is None
                or business.default_phone_region != snapshot.default_phone_region
            ):
                result = _create_result(
                    CRMContactCreateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.LOCAL_STATE_CHANGED,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                _store_uncertain(session, snapshot, execution, result)
                return result
            existing_link = await session.get(CRMContactLink, snapshot.contact_id)
            if existing_link is not None:
                if (
                    existing_link.hubspot_portal_id == snapshot.portal_id
                    and existing_link.hubspot_contact_id == provider_contact.id
                    and existing_link.state is CRMContactLinkState.ACTIVE
                ):
                    result = _create_result(
                        CRMContactCreateOutcome.REPLAYED,
                        provider_id=provider_contact.id,
                        replayed=True,
                    )
                    _terminalize(session, snapshot, execution, result)
                    return result
                result = _create_result(
                    CRMContactCreateOutcome.UNCERTAIN,
                    replayed=True,
                    error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
                _store_uncertain(session, snapshot, execution, result)
                return result

            result = _create_result(
                CRMContactCreateOutcome.REPLAYED,
                provider_id=provider_contact.id,
                replayed=True,
            )
            _persist_local_link(session, contact, snapshot, provider_contact, verified.linked_by)
            await session.flush()
            _terminalize(session, snapshot, execution, result)
            return result
    except (IntegrityError, SQLAlchemyError):
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=True,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )


async def _complete_provider_failure(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    error: HubSpotProviderError,
) -> CRMContactCreateResult:
    result = _provider_error_result(
        error,
        uncertain=snapshot.recovery or error.outcome_uncertain,
        replayed=snapshot.replayed,
    )
    if result.outcome is CRMContactCreateOutcome.UNCERTAIN:
        return await _record_uncertain(session_factory, snapshot, result)
    return await _complete_result(session_factory, snapshot, result)


async def _finish_prewrite_result(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    result: CRMContactCreateResult,
) -> CRMContactCreateResult:
    if snapshot.recovery:
        action = (
            CRMContactWriteNextAction.OPERATOR_REVIEW
            if result.next_action is CRMContactWriteNextAction.OPERATOR_REVIEW
            else CRMContactWriteNextAction.RECONCILE_SAME_KEY
        )
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=True,
                error_code=result.error_code,
                next_action=action,
            ),
        )
    return await _complete_result(session_factory, snapshot, result)


def _provider_error_result(
    error: HubSpotProviderError,
    *,
    uncertain: bool,
    replayed: bool,
) -> CRMContactCreateResult:
    if uncertain:
        code = (
            CRMContactWriteErrorCode.PERMISSION_DENIED
            if error.code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}
            else CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
            if error.code is HubSpotErrorCode.INVALID_RESPONSE
            else CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
        )
        return _create_result(
            CRMContactCreateOutcome.UNCERTAIN,
            replayed=replayed,
            error_code=code,
            next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
        )
    if error.code in {HubSpotErrorCode.NOT_CONFIGURED, HubSpotErrorCode.LIVE_TRANSPORT_DISABLED}:
        return _create_result(
            CRMContactCreateOutcome.NOT_CONFIGURED,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.NOT_CONFIGURED,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if error.code is HubSpotErrorCode.ACTIVATION_REQUIRED:
        return _create_result(
            CRMContactCreateOutcome.NOT_CONFIGURED,
            replayed=replayed,
            error_code=CRMContactWriteErrorCode.ACTIVATION_REQUIRED,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if error.code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}:
        code = CRMContactWriteErrorCode.PERMISSION_DENIED
    elif error.code is HubSpotErrorCode.VALIDATION:
        code = CRMContactWriteErrorCode.PROVIDER_VALIDATION
    elif error.code is HubSpotErrorCode.RATE_LIMITED:
        code = CRMContactWriteErrorCode.RATE_LIMITED
    elif error.code is HubSpotErrorCode.INVALID_RESPONSE:
        code = CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
    elif error.code is HubSpotErrorCode.CONFLICT:
        code = CRMContactWriteErrorCode.IDENTITY_CONFLICT
    else:
        code = CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
    return _create_result(
        CRMContactCreateOutcome.FAILED,
        replayed=replayed,
        error_code=code,
        next_action=(
            CRMContactWriteNextAction.OPERATOR_REVIEW
            if code
            in {
                CRMContactWriteErrorCode.PERMISSION_DENIED,
                CRMContactWriteErrorCode.PROVIDER_VALIDATION,
                CRMContactWriteErrorCode.IDENTITY_CONFLICT,
                CRMContactWriteErrorCode.RATE_LIMITED,
            }
            else CRMContactWriteNextAction.RETRY_SAME_KEY
        ),
    )


def _preflight_failure(result: HubSpotPreflightResult) -> CRMContactCreateResult | None:
    if result.outcome is HubSpotPreflightOutcome.VERIFIED and result.account_identity_verified:
        return None
    if result.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH:
        return _create_result(
            CRMContactCreateOutcome.NOT_CONFIGURED,
            error_code=CRMContactWriteErrorCode.ACCOUNT_MISMATCH,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if result.outcome is HubSpotPreflightOutcome.PERMISSION_DENIED:
        return _create_result(
            CRMContactCreateOutcome.FAILED,
            error_code=CRMContactWriteErrorCode.PERMISSION_DENIED,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    code = (
        CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE
        if result.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE
        else CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE
        if result.outcome is HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE
        else CRMContactWriteErrorCode.NOT_CONFIGURED
    )
    return _create_result(
        CRMContactCreateOutcome.NOT_CONFIGURED
        if code is CRMContactWriteErrorCode.NOT_CONFIGURED
        else CRMContactCreateOutcome.FAILED,
        error_code=code,
        next_action=(
            CRMContactWriteNextAction.OPERATOR_REVIEW
            if code is CRMContactWriteErrorCode.NOT_CONFIGURED
            else CRMContactWriteNextAction.RETRY_SAME_KEY
        ),
    )


async def _complete_result(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    result: CRMContactCreateResult,
) -> CRMContactCreateResult:
    try:
        async with session_factory.begin() as session:
            await session.scalar(
                select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
            )
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            if execution is None:
                return _create_result(
                    CRMContactCreateOutcome.FAILED,
                    replayed=snapshot.replayed,
                    error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                    next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
                )
            if execution.status is not ToolExecutionStatus.STARTED:
                return _stored_replay(execution)
            _terminalize(session, snapshot, execution, result)
            return result
    except SQLAlchemyError:
        return await _record_uncertain(
            session_factory,
            snapshot,
            _create_result(
                CRMContactCreateOutcome.UNCERTAIN,
                replayed=snapshot.replayed,
                error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
            ),
        )


async def _record_uncertain(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot: _CreateSnapshot,
    result: CRMContactCreateResult,
) -> CRMContactCreateResult:
    returned = result
    try:
        async with session_factory.begin() as session:
            await session.scalar(
                select(Contact).where(Contact.id == snapshot.contact_id).with_for_update()
            )
            execution = await session.scalar(
                select(ToolExecution)
                .where(ToolExecution.id == snapshot.execution_id)
                .with_for_update()
            )
            if execution is None:
                return result
            if execution.status is not ToolExecutionStatus.STARTED:
                returned = _stored_replay(execution)
            else:
                _store_uncertain(session, snapshot, execution, result)
    except SQLAlchemyError:
        pass
    return returned


def _terminalize(
    session: AsyncSession,
    snapshot: _CreateSnapshot,
    execution: ToolExecution,
    result: CRMContactCreateResult,
) -> None:
    execution.status = _execution_status(result.outcome)
    execution.sanitized_result = result.model_dump(mode="json")
    execution.safe_error_code = None if result.error_code is None else result.error_code.value
    execution.safe_error_message = (
        None if result.error_code is None else "CRM Contact creation did not complete"
    )
    execution.finished_at = datetime.now(UTC)
    session.add(
        _audit_event(
            snapshot,
            result.outcome.value,
            replayed=result.replayed,
            provider_id=result.hubspot_contact_id,
        )
    )


def _store_uncertain(
    session: AsyncSession,
    snapshot: _CreateSnapshot,
    execution: ToolExecution,
    result: CRMContactCreateResult,
) -> None:
    execution.status = ToolExecutionStatus.STARTED
    execution.sanitized_result = result.model_dump(mode="json")
    execution.safe_error_code = None if result.error_code is None else result.error_code.value
    execution.safe_error_message = "CRM Contact creation requires same-key reconciliation"
    execution.finished_at = None
    session.add(_audit_event(snapshot, "uncertain", replayed=result.replayed))


def _execution_status(outcome: CRMContactCreateOutcome) -> ToolExecutionStatus:
    if outcome in {
        CRMContactCreateOutcome.CREATED,
        CRMContactCreateOutcome.EXISTING_CONTACT,
        CRMContactCreateOutcome.REPLAYED,
    }:
        return ToolExecutionStatus.SUCCEEDED
    if outcome in {
        CRMContactCreateOutcome.NOT_CONFIGURED,
        CRMContactCreateOutcome.INVALID_REQUEST,
        CRMContactCreateOutcome.OWNERSHIP_REJECTED,
        CRMContactCreateOutcome.IDENTITY_CONFLICT,
        CRMContactCreateOutcome.IDEMPOTENCY_CONFLICT,
    }:
        return ToolExecutionStatus.REJECTED
    return ToolExecutionStatus.FAILED


def _audit_event(
    snapshot: _CreateSnapshot,
    outcome: str,
    *,
    replayed: bool,
    provider_id: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_type=_OPERATION,
        actor_type=AuditActorType.SYSTEM,
        contact_id=snapshot.contact_id,
        conversation_id=snapshot.conversation_id,
        entity_type="hubspot_contact",
        entity_identifier=provider_id or str(snapshot.contact_id),
        sanitized_metadata={
            "operation": _OPERATION,
            "outcome": outcome,
            "tool_execution_id": str(snapshot.execution_id),
            "replayed": replayed,
        },
    )


def _stored_replay(execution: ToolExecution) -> CRMContactCreateResult:
    if isinstance(execution.sanitized_result, dict):
        try:
            stored = CRMContactCreateResult.model_validate(execution.sanitized_result)
        except ValueError:
            stored = None
        if stored is not None:
            if stored.outcome in {
                CRMContactCreateOutcome.CREATED,
                CRMContactCreateOutcome.EXISTING_CONTACT,
            }:
                return stored.model_copy(
                    update={"outcome": CRMContactCreateOutcome.REPLAYED, "replayed": True}
                )
            return stored.model_copy(update={"replayed": True})
    return _create_result(
        CRMContactCreateOutcome.OPERATION_IN_PROGRESS,
        replayed=True,
        error_code=CRMContactWriteErrorCode.OPERATION_IN_PROGRESS,
        next_action=CRMContactWriteNextAction.RECONCILE_SAME_KEY,
    )


def _retryable_no_write_failure(execution: ToolExecution) -> bool:
    """Allow explicit same-key retry only when a prior result proves no write."""
    if execution.status is not ToolExecutionStatus.FAILED:
        return False
    if not isinstance(execution.sanitized_result, dict):
        return False
    try:
        result = CRMContactCreateResult.model_validate(execution.sanitized_result)
    except ValueError:
        return False
    return (
        result.outcome is CRMContactCreateOutcome.FAILED
        and result.next_action is CRMContactWriteNextAction.RETRY_SAME_KEY
        and result.error_code
        in {
            CRMContactWriteErrorCode.PROVIDER_UNAVAILABLE,
            CRMContactWriteErrorCode.INVALID_PROVIDER_RESPONSE,
        }
    )


def _validation_failure(
    validation: CRMContactWriteValidationResult,
) -> CRMContactCreateResult:
    if validation.outcome is CRMContactWriteValidationOutcome.OWNERSHIP_MISMATCH:
        outcome = CRMContactCreateOutcome.OWNERSHIP_REJECTED
    elif validation.outcome is CRMContactWriteValidationOutcome.IDENTITY_CONFLICT:
        outcome = CRMContactCreateOutcome.IDENTITY_CONFLICT
    elif validation.outcome is CRMContactWriteValidationOutcome.FAILED:
        outcome = CRMContactCreateOutcome.FAILED
    else:
        outcome = CRMContactCreateOutcome.INVALID_REQUEST
    return _create_result(
        outcome,
        error_code=validation.error_code,
        next_action=validation.next_action,
    )


def _create_result(
    outcome: CRMContactCreateOutcome,
    *,
    provider_id: str | None = None,
    replayed: bool = False,
    error_code: CRMContactWriteErrorCode | None = None,
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE,
) -> CRMContactCreateResult:
    if outcome is CRMContactCreateOutcome.REPLAYED:
        replayed = True
    return CRMContactCreateResult(
        outcome=outcome,
        hubspot_contact_id=provider_id,
        replayed=replayed,
        error_code=error_code,
        next_action=next_action,
    )


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > _EMAIL_LIMIT
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _settings_structurally_configured(settings: Settings) -> bool:
    key = settings.hubspot_service_key
    portal = settings.hubspot_portal_id
    return (
        key is not None
        and bool(key.get_secret_value().strip())
        and portal is not None
        and portal.isascii()
        and portal.isdecimal()
        and int(portal) > 0
    )


def _configuration_unchanged(
    settings: Settings,
    expected_key: SecretStr | None,
    expected_portal: str | None,
) -> bool:
    return (
        settings.hubspot_service_key is expected_key
        and settings.hubspot_portal_id == expected_portal
        and _settings_structurally_configured(settings)
    )

"""Record one verified historical booking operation as a HubSpot Note."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.application.booking import ToolExecutionClaimOutcome, _claim_tool_execution
from receptionist.application.crm_booking_evidence import (
    BookingEvidence,
    BookingEvidenceValidationOutcome,
    BookingEvidenceValidationRequest,
    validate_booking_evidence,
)
from receptionist.application.crm_identity import (
    CRMContactIdentityOutcome,
    decide_crm_contact_identity,
)
from receptionist.core.config import Settings
from receptionist.db.models import (
    AuditEvent,
    Booking,
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
    HubSpotCapabilityStatus,
    HubSpotClient,
    HubSpotContactSearchResult,
    HubSpotContactSnapshot,
    HubSpotErrorCode,
    HubSpotNoteSnapshot,
    HubSpotPreflightOutcome,
    HubSpotPreflightResult,
    HubSpotProviderError,
)

_OPERATION: Literal["record_booking_outcome"] = "record_booking_outcome"
_KEY_NAMESPACE = "crm:record_booking_outcome:v1:"
_SOURCE_MARKER_PREFIX = "booking-execution:"
_CRM_MUTATIONS = ("create_crm_contact", "update_crm_contact", _OPERATION)


class _BookingOutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class BookingOutcomeRequest(_BookingOutcomeModel):
    """Only the local Booking and immutable source execution may be supplied."""

    booking_id: UUID
    source_tool_execution_id: UUID


class BookingOutcome(StrEnum):
    WRITTEN = "written"
    REPLAYED = "replayed"
    SKIPPED_NO_VERIFIED_CONTACT = "skipped_no_verified_contact"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


class BookingOutcomeErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    SOURCE_RECORD_MISSING = "source_record_missing"
    SOURCE_EXECUTION_INVALID = "source_execution_invalid"
    HISTORICAL_RESULT_MISMATCH = "historical_result_mismatch"
    CONTACT_OWNERSHIP_CONFLICT = "contact_ownership_conflict"
    NO_VERIFIED_CONTACT = "no_verified_contact"
    ACCOUNT_MISMATCH = "account_mismatch"
    ACTIVATION_REQUIRED = "activation_required"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_VALIDATION = "provider_validation"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    NOTE_IDENTITY_UNVERIFIED = "note_identity_unverified"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    LOCAL_STATE_CHANGED = "local_state_changed"
    LOCAL_PERSISTENCE_FAILURE = "local_persistence_failure"


class BookingOutcomeNextAction(StrEnum):
    NONE = "none"
    RETRY_SAME_KEY = "retry_same_key"
    RECONCILE_SAME_KEY = "reconcile_same_key"
    OPERATOR_REVIEW = "operator_review"


class BookingOutcomeResult(_BookingOutcomeModel):
    operation: Literal["record_booking_outcome"] = _OPERATION
    outcome: BookingOutcome
    hubspot_note_id: str | None = Field(default=None, min_length=1, max_length=255)
    replayed: bool = False
    error_code: BookingOutcomeErrorCode | None = None
    next_action: BookingOutcomeNextAction = BookingOutcomeNextAction.NONE

    @model_validator(mode="after")
    def replay_outcome_requires_replayed_flag(self) -> BookingOutcomeResult:
        if self.outcome is BookingOutcome.REPLAYED and not self.replayed:
            raise ValueError("replayed outcome requires replayed flag")
        return self


@dataclass(frozen=True, slots=True)
class _ContactBinding:
    contact_id: UUID
    conversation_id: UUID
    portal_id: str
    hubspot_contact_id: str
    linked_by: CRMContactLinkProvenance
    phone_e164: str | None
    verified_email: str | None
    default_phone_region: str | None


def _result(
    outcome: BookingOutcome,
    *,
    note_id: str | None = None,
    replayed: bool = False,
    error_code: BookingOutcomeErrorCode | None = None,
    next_action: BookingOutcomeNextAction = BookingOutcomeNextAction.NONE,
) -> BookingOutcomeResult:
    return BookingOutcomeResult(
        outcome=outcome,
        hubspot_note_id=note_id,
        replayed=replayed,
        error_code=error_code,
        next_action=next_action,
    )


def _note_idempotency_key(source_tool_execution_id: UUID) -> str:
    return f"{_KEY_NAMESPACE}{source_tool_execution_id}"


def _source_operation_marker(source_tool_execution_id: UUID) -> str:
    return f"{_SOURCE_MARKER_PREFIX}{source_tool_execution_id}"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_booking_note(evidence: BookingEvidence) -> str:
    """Return the one canonical, bounded Note representation for source evidence."""
    lines = [
        f"Booking operation: {evidence.action.value}",
        f"Outcome: {evidence.historical_status.value}",
        f"Booking reference: {evidence.booking_id}",
    ]
    if evidence.requested_start_at_utc is not None:
        lines.append(f"Requested start (UTC): {_utc_iso(evidence.requested_start_at_utc)}")
    if evidence.requested_end_at_utc is not None:
        lines.append(f"Requested end (UTC): {_utc_iso(evidence.requested_end_at_utc)}")
    lines.append(f"Source operation: {_source_operation_marker(evidence.source_tool_execution_id)}")
    return "\n".join(lines)


def _claim_arguments(evidence: BookingEvidence, binding: _ContactBinding) -> dict[str, object]:
    return {
        "source_tool_execution_id": str(evidence.source_tool_execution_id),
        "booking_id": str(evidence.booking_id),
        "contact_id": str(evidence.contact_id),
        "conversation_id": str(evidence.conversation_id),
        "hubspot_portal_id": binding.portal_id,
        "hubspot_contact_id": binding.hubspot_contact_id,
        "operation_type": _OPERATION,
        "historical_action": evidence.action.value,
        "historical_status": evidence.historical_status.value,
        "requested_start_at_utc": (
            None
            if evidence.requested_start_at_utc is None
            else _utc_iso(evidence.requested_start_at_utc)
        ),
        "requested_end_at_utc": (
            None
            if evidence.requested_end_at_utc is None
            else _utc_iso(evidence.requested_end_at_utc)
        ),
        "source_finished_at_utc": _utc_iso(evidence.source_finished_at_utc),
        "source_marker": _source_operation_marker(evidence.source_tool_execution_id),
    }


async def record_booking_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    request: BookingOutcomeRequest,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BookingOutcomeResult:
    """Write one structured Note from a validated, committed historical source."""
    if not isinstance(request, BookingOutcomeRequest):
        return _result(
            BookingOutcome.FAILED,
            error_code=BookingOutcomeErrorCode.INVALID_REQUEST,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )

    validation = await validate_booking_evidence(
        session_factory,
        BookingEvidenceValidationRequest(
            booking_id=request.booking_id,
            source_tool_execution_id=request.source_tool_execution_id,
        ),
    )
    if validation.outcome is not BookingEvidenceValidationOutcome.VALID:
        return _validation_failure(validation.outcome)
    evidence = validation.evidence
    if evidence is None:
        return _result(
            BookingOutcome.FAILED,
            error_code=BookingOutcomeErrorCode.HISTORICAL_RESULT_MISMATCH,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )

    idempotency_key = _note_idempotency_key(evidence.source_tool_execution_id)
    existing = await _existing_claim(session_factory, evidence, idempotency_key)
    if existing is not None:
        return existing

    if not _settings_configured(settings):
        return _result(
            BookingOutcome.NOT_CONFIGURED,
            error_code=BookingOutcomeErrorCode.ACTIVATION_REQUIRED,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )
    expected_key = settings.hubspot_service_key
    expected_portal = settings.hubspot_portal_id
    try:
        binding = await _load_contact_binding(
            session_factory,
            evidence,
            expected_portal_id=expected_portal,
        )
    except SQLAlchemyError:
        return _local_persistence_failure()
    if binding is None:
        return _no_verified_contact()

    try:
        preflight_client = HubSpotClient(settings, transport=transport)
    except HubSpotProviderError as error:
        return _prewrite_provider_failure(error)

    operation_client: HubSpotClient | None = None
    try:
        preflight = await preflight_client.preflight_account()
        failure = _preflight_failure(preflight)
        if failure is not None:
            return failure
        if not _settings_unchanged(settings, expected_key, expected_portal):
            return _result(
                BookingOutcome.NOT_CONFIGURED,
                error_code=BookingOutcomeErrorCode.ACCOUNT_MISMATCH,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )

        mock_transport_only = settings.app_env in {"test", "ci"} and isinstance(
            transport, httpx.MockTransport
        )
        if preflight.writes_enabled:
            operation_client = preflight_client
        elif mock_transport_only:
            # M3A permits isolated MockTransport writes without enabling production capability.
            operation_client = HubSpotClient(settings, transport=transport)
        else:
            return _result(
                BookingOutcome.NOT_CONFIGURED,
                error_code=BookingOutcomeErrorCode.ACTIVATION_REQUIRED,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )

        provider_contact = await operation_client.get_contact_by_id(binding.hubspot_contact_id)
        if provider_contact.archived or provider_contact.id != binding.hubspot_contact_id:
            return _no_verified_contact()
        identity_verified = await _verify_contact_identity(
            operation_client,
            binding,
            provider_contact,
        )
        if identity_verified is not None:
            return identity_verified
        if not _settings_unchanged(settings, expected_key, expected_portal):
            return _result(
                BookingOutcome.NOT_CONFIGURED,
                error_code=BookingOutcomeErrorCode.ACCOUNT_MISMATCH,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )

        second_validation = await validate_booking_evidence(
            session_factory,
            BookingEvidenceValidationRequest(
                booking_id=request.booking_id,
                source_tool_execution_id=request.source_tool_execution_id,
            ),
        )
        if second_validation.outcome is not BookingEvidenceValidationOutcome.VALID:
            return _validation_failure(second_validation.outcome)
        if second_validation.evidence != evidence:
            return _result(
                BookingOutcome.FAILED,
                error_code=BookingOutcomeErrorCode.LOCAL_STATE_CHANGED,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )

        claim = await _create_claim(
            session_factory,
            evidence,
            binding,
            idempotency_key=idempotency_key,
        )
        if isinstance(claim, BookingOutcomeResult):
            return claim

        binding_unchanged = await _binding_is_current(
            session_factory,
            evidence,
            binding,
            expected_portal_id=expected_portal,
        )
        if not binding_unchanged or not _settings_unchanged(
            settings, expected_key, expected_portal
        ):
            result = _result(
                BookingOutcome.FAILED,
                error_code=BookingOutcomeErrorCode.LOCAL_STATE_CHANGED,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )
            return await _persist_claim_result(session_factory, claim, evidence, result)

        final_validation = await validate_booking_evidence(
            session_factory,
            BookingEvidenceValidationRequest(
                booking_id=request.booking_id,
                source_tool_execution_id=request.source_tool_execution_id,
            ),
        )
        if final_validation.outcome is not BookingEvidenceValidationOutcome.VALID:
            return await _persist_claim_result(
                session_factory,
                claim,
                evidence,
                _validation_failure(final_validation.outcome),
            )
        if final_validation.evidence != evidence:
            result = _result(
                BookingOutcome.FAILED,
                error_code=BookingOutcomeErrorCode.LOCAL_STATE_CHANGED,
                next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
            )
            return await _persist_claim_result(session_factory, claim, evidence, result)

        note_body = _format_booking_note(evidence)
        try:
            created = await operation_client.create_note(
                binding.hubspot_contact_id,
                body=note_body,
                timestamp=evidence.source_finished_at_utc,
            )
        except HubSpotProviderError as error:
            result = _post_provider_failure(error)
            return await _persist_claim_result(session_factory, claim, evidence, result)

        verified_note = await _verify_created_note(
            operation_client,
            created,
            expected_body=note_body,
            expected_timestamp=evidence.source_finished_at_utc,
            expected_contact_id=binding.hubspot_contact_id,
        )
        if verified_note is None:
            result = _result(
                BookingOutcome.UNCERTAIN,
                error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
                next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
            )
            return await _persist_claim_result(session_factory, claim, evidence, result)

        result = _result(BookingOutcome.WRITTEN, note_id=verified_note.id)
        return await _persist_claim_result(session_factory, claim, evidence, result)
    except HubSpotProviderError as error:
        return _prewrite_provider_failure(error)
    except SQLAlchemyError:
        return _local_persistence_failure()
    finally:
        if operation_client is not None and operation_client is not preflight_client:
            await operation_client.aclose()
        await preflight_client.aclose()


def _validation_failure(outcome: BookingEvidenceValidationOutcome) -> BookingOutcomeResult:
    if outcome is BookingEvidenceValidationOutcome.MISSING_SOURCE_RECORD:
        error = BookingOutcomeErrorCode.SOURCE_RECORD_MISSING
    elif outcome is BookingEvidenceValidationOutcome.INVALID_SOURCE_EXECUTION:
        error = BookingOutcomeErrorCode.SOURCE_EXECUTION_INVALID
    elif outcome is BookingEvidenceValidationOutcome.OWNERSHIP_CONFLICT:
        error = BookingOutcomeErrorCode.CONTACT_OWNERSHIP_CONFLICT
    elif outcome is BookingEvidenceValidationOutcome.LOCAL_PERSISTENCE_FAILURE:
        return _local_persistence_failure()
    else:
        error = BookingOutcomeErrorCode.HISTORICAL_RESULT_MISMATCH
    return _result(
        BookingOutcome.FAILED,
        error_code=error,
        next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
    )


async def _existing_claim(
    session_factory: async_sessionmaker[AsyncSession],
    evidence: BookingEvidence,
    idempotency_key: str,
) -> BookingOutcomeResult | None:
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecution).where(ToolExecution.idempotency_key == idempotency_key)
            )
    except SQLAlchemyError:
        return _local_persistence_failure()
    if execution is None:
        return None
    if execution.tool_name != _OPERATION or execution.conversation_id != evidence.conversation_id:
        return _result(
            BookingOutcome.FAILED,
            replayed=True,
            error_code=BookingOutcomeErrorCode.IDEMPOTENCY_CONFLICT,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )
    arguments = execution.sanitized_arguments
    if not isinstance(arguments, Mapping) or not _source_binding_matches(arguments, evidence):
        return _result(
            BookingOutcome.FAILED,
            replayed=True,
            error_code=BookingOutcomeErrorCode.IDEMPOTENCY_CONFLICT,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )
    if execution.status is ToolExecutionStatus.STARTED:
        return _result(
            BookingOutcome.UNCERTAIN,
            replayed=True,
            error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    stored = execution.sanitized_result
    if isinstance(stored, Mapping):
        code = stored.get("error_code")
        action = stored.get("next_action")
        if (
            execution.status is ToolExecutionStatus.FAILED
            and stored.get("outcome") == BookingOutcome.FAILED.value
            and code == BookingOutcomeErrorCode.PROVIDER_UNAVAILABLE.value
            and action == BookingOutcomeNextAction.RETRY_SAME_KEY.value
        ):
            return None
    try:
        prior = BookingOutcomeResult.model_validate(stored)
    except (TypeError, ValueError):
        return _result(
            BookingOutcome.UNCERTAIN,
            replayed=True,
            error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    if (
        execution.status is ToolExecutionStatus.SUCCEEDED
        and prior.outcome is BookingOutcome.WRITTEN
    ):
        return _result(
            BookingOutcome.REPLAYED,
            note_id=prior.hubspot_note_id,
            replayed=True,
        )
    if execution.status is ToolExecutionStatus.FAILED and prior.outcome is BookingOutcome.FAILED:
        return prior.model_copy(update={"replayed": True})
    return _result(
        BookingOutcome.UNCERTAIN,
        replayed=True,
        error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
        next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
    )


def _source_binding_matches(arguments: Mapping[str, object], evidence: BookingEvidence) -> bool:
    expected = {
        "source_tool_execution_id": str(evidence.source_tool_execution_id),
        "booking_id": str(evidence.booking_id),
        "contact_id": str(evidence.contact_id),
        "conversation_id": str(evidence.conversation_id),
        "operation_type": _OPERATION,
        "historical_action": evidence.action.value,
        "historical_status": evidence.historical_status.value,
        "requested_start_at_utc": (
            None
            if evidence.requested_start_at_utc is None
            else _utc_iso(evidence.requested_start_at_utc)
        ),
        "requested_end_at_utc": (
            None
            if evidence.requested_end_at_utc is None
            else _utc_iso(evidence.requested_end_at_utc)
        ),
        "source_finished_at_utc": _utc_iso(evidence.source_finished_at_utc),
        "source_marker": _source_operation_marker(evidence.source_tool_execution_id),
    }
    return all(arguments.get(key) == value for key, value in expected.items()) and bool(
        arguments.get("hubspot_portal_id") and arguments.get("hubspot_contact_id")
    )


async def _load_contact_binding(
    session_factory: async_sessionmaker[AsyncSession],
    evidence: BookingEvidence,
    *,
    expected_portal_id: str | None,
) -> _ContactBinding | None:
    async with session_factory() as session:
        contact = await session.get(Contact, evidence.contact_id)
        conversation_contact_id = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == evidence.conversation_id)
        )
        booking_contact_id = await session.scalar(
            select(Booking.contact_id).where(Booking.id == evidence.booking_id)
        )
        link = await session.get(CRMContactLink, evidence.contact_id)
        region = await session.scalar(
            select(BusinessConfig.default_phone_region).where(BusinessConfig.id == 1)
        )
    if (
        contact is None
        or link is None
        or conversation_contact_id != evidence.contact_id
        or booking_contact_id != evidence.contact_id
        or link.state is not CRMContactLinkState.ACTIVE
        or link.hubspot_portal_id != expected_portal_id
        or not _valid_provider_id(link.hubspot_contact_id)
    ):
        return None
    if link.linked_by is CRMContactLinkProvenance.PHONE_E164:
        if contact.phone_e164 is None or region is None:
            return None
        verified_email = None
    elif link.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL:
        verified_email = _normalize_email(contact.email)
        if verified_email is None or (contact.phone_e164 is not None and region is None):
            return None
    else:
        return None
    return _ContactBinding(
        contact_id=evidence.contact_id,
        conversation_id=evidence.conversation_id,
        portal_id=link.hubspot_portal_id,
        hubspot_contact_id=link.hubspot_contact_id,
        linked_by=link.linked_by,
        phone_e164=contact.phone_e164,
        verified_email=verified_email,
        default_phone_region=region,
    )


async def _verify_contact_identity(
    client: HubSpotClient,
    binding: _ContactBinding,
    provider_contact: HubSpotContactSnapshot,
) -> BookingOutcomeResult | None:
    phone_search = None
    if binding.linked_by is CRMContactLinkProvenance.PHONE_E164:
        if binding.phone_e164 is None:
            return _no_verified_contact()
        direct_identity = decide_crm_contact_identity(
            local_phone_e164=binding.phone_e164,
            trusted_verified_email=None,
            phone_search=HubSpotContactSearchResult((provider_contact,), False, None),
            email_match=None,
            email_lookup_complete=True,
            default_region=binding.default_phone_region or "",
        )
        if (
            direct_identity.outcome is not CRMContactIdentityOutcome.MATCHED
            or direct_identity.hubspot_contact_id != binding.hubspot_contact_id
        ):
            return _no_verified_contact()
        phone_search = await client.search_contacts_by_phone(binding.phone_e164)
        email_match = None
        trusted_email = None
    else:
        if binding.verified_email is None:
            return _no_verified_contact()
        try:
            email_lookup = await client.get_contact_by_email(binding.verified_email)
        except HubSpotProviderError as error:
            if error.code is HubSpotErrorCode.NOT_FOUND:
                return _no_verified_contact()
            raise
        if (
            email_lookup.id != binding.hubspot_contact_id
            or _normalize_email(provider_contact.email) != binding.verified_email
            or _normalize_email(email_lookup.email) != binding.verified_email
        ):
            return _no_verified_contact()
        email_match = provider_contact
        trusted_email = binding.verified_email
        if binding.phone_e164 is not None:
            phone_search = await client.search_contacts_by_phone(binding.phone_e164)

    decision = decide_crm_contact_identity(
        local_phone_e164=binding.phone_e164,
        trusted_verified_email=trusted_email,
        phone_search=phone_search,
        email_match=email_match,
        email_lookup_complete=True,
        default_region=binding.default_phone_region or "",
    )
    if (
        decision.outcome is CRMContactIdentityOutcome.MATCHED
        and decision.hubspot_contact_id == binding.hubspot_contact_id
    ):
        return None
    return _no_verified_contact()


def _normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > 254
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _valid_provider_id(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or not value.isascii()
        or not value.isdecimal()
    ):
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


async def _binding_is_current(
    session_factory: async_sessionmaker[AsyncSession],
    evidence: BookingEvidence,
    expected: _ContactBinding,
    *,
    expected_portal_id: str | None,
) -> bool:
    current = await _load_contact_binding(
        session_factory,
        evidence,
        expected_portal_id=expected_portal_id,
    )
    return current == expected


async def _create_claim(
    session_factory: async_sessionmaker[AsyncSession],
    evidence: BookingEvidence,
    binding: _ContactBinding,
    *,
    idempotency_key: str,
) -> ToolExecution | BookingOutcomeResult:
    try:
        async with session_factory.begin() as session:
            contact = await session.scalar(
                select(Contact).where(Contact.id == evidence.contact_id).with_for_update()
            )
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.id == evidence.conversation_id)
                .with_for_update()
            )
            booking_contact_id = await session.scalar(
                select(Booking.contact_id).where(Booking.id == evidence.booking_id)
            )
            link = await session.scalar(
                select(CRMContactLink)
                .where(CRMContactLink.contact_id == evidence.contact_id)
                .with_for_update()
            )
            email_changed = (
                binding.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL
                and _normalize_email(contact.email if contact is not None else None)
                != binding.verified_email
            )
            if (
                contact is None
                or conversation is None
                or conversation.contact_id != evidence.contact_id
                or booking_contact_id != evidence.contact_id
                or link is None
                or link.state is not CRMContactLinkState.ACTIVE
                or link.hubspot_portal_id != binding.portal_id
                or link.hubspot_contact_id != binding.hubspot_contact_id
                or link.linked_by is not binding.linked_by
                or contact.phone_e164 != binding.phone_e164
                or email_changed
            ):
                return _result(
                    BookingOutcome.FAILED,
                    error_code=BookingOutcomeErrorCode.LOCAL_STATE_CHANGED,
                    next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
                )

            active = await session.scalar(
                select(ToolExecution.id)
                .where(
                    ToolExecution.conversation_id.in_(
                        select(Conversation.id).where(
                            Conversation.contact_id == evidence.contact_id
                        )
                    ),
                    ToolExecution.tool_name.in_(_CRM_MUTATIONS),
                    ToolExecution.status == ToolExecutionStatus.STARTED,
                    ToolExecution.idempotency_key != idempotency_key,
                )
                .limit(1)
            )
            if active is not None:
                return _result(
                    BookingOutcome.FAILED,
                    error_code=BookingOutcomeErrorCode.OPERATION_IN_PROGRESS,
                    next_action=BookingOutcomeNextAction.RETRY_SAME_KEY,
                )

            arguments = _claim_arguments(evidence, binding)
            claim = await _claim_tool_execution(
                session,
                conversation_id=evidence.conversation_id,
                tool_name=_OPERATION,
                idempotency_key=idempotency_key,
                sanitized_arguments=arguments,
            )
            if claim.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT:
                return _result(
                    BookingOutcome.FAILED,
                    replayed=True,
                    error_code=BookingOutcomeErrorCode.IDEMPOTENCY_CONFLICT,
                    next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
                )
            if claim.outcome is ToolExecutionClaimOutcome.NEW:
                execution = claim.execution
            elif claim.outcome is ToolExecutionClaimOutcome.FAILED_REPLAY:
                stored = claim.execution.sanitized_result
                if not _known_no_write_rate_limit(stored):
                    return _replay_claim(claim.execution)
                execution = claim.execution
                execution.status = ToolExecutionStatus.STARTED
                execution.sanitized_result = None
                execution.safe_error_code = None
                execution.safe_error_message = None
                execution.finished_at = None
            else:
                return _replay_claim(claim.execution)
            session.add(_audit_event(evidence, "started"))
        return execution
    except SQLAlchemyError:
        return _local_persistence_failure()


def _known_no_write_rate_limit(stored: object) -> bool:
    return (
        isinstance(stored, Mapping)
        and stored.get("outcome") == BookingOutcome.FAILED.value
        and stored.get("error_code") == BookingOutcomeErrorCode.PROVIDER_UNAVAILABLE.value
        and stored.get("next_action") == BookingOutcomeNextAction.RETRY_SAME_KEY.value
    )


def _replay_claim(execution: ToolExecution) -> BookingOutcomeResult:
    if execution.status is ToolExecutionStatus.STARTED:
        return _result(
            BookingOutcome.UNCERTAIN,
            replayed=True,
            error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    try:
        prior = BookingOutcomeResult.model_validate(execution.sanitized_result)
    except (TypeError, ValueError):
        return _result(
            BookingOutcome.UNCERTAIN,
            replayed=True,
            error_code=BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED,
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    if (
        execution.status is ToolExecutionStatus.SUCCEEDED
        and prior.outcome is BookingOutcome.WRITTEN
    ):
        return _result(
            BookingOutcome.REPLAYED,
            note_id=prior.hubspot_note_id,
            replayed=True,
        )
    return prior.model_copy(update={"replayed": True})


async def _verify_created_note(
    client: HubSpotClient,
    created: HubSpotNoteSnapshot,
    *,
    expected_body: str,
    expected_timestamp: datetime,
    expected_contact_id: str,
) -> HubSpotNoteSnapshot | None:
    candidate = created
    if not _note_is_verified(
        candidate,
        expected_body=expected_body,
        expected_timestamp=expected_timestamp,
        expected_contact_id=expected_contact_id,
    ):
        try:
            candidate = await client.get_note(created.id)
        except HubSpotProviderError:
            return None
    if not _note_is_verified(
        candidate,
        expected_body=expected_body,
        expected_timestamp=expected_timestamp,
        expected_contact_id=expected_contact_id,
    ):
        return None
    return candidate


def _note_is_verified(
    note: HubSpotNoteSnapshot,
    *,
    expected_body: str,
    expected_timestamp: datetime,
    expected_contact_id: str,
) -> bool:
    if (
        not _valid_provider_id(note.id)
        or note.archived
        or note.body != expected_body
        or note.timestamp is None
        or note.contact_ids != (expected_contact_id,)
    ):
        return False
    timestamp = _parse_aware_timestamp(note.timestamp)
    return timestamp == expected_timestamp.astimezone(UTC)


def _parse_aware_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _preflight_failure(preflight: HubSpotPreflightResult) -> BookingOutcomeResult | None:
    if (
        preflight.outcome is HubSpotPreflightOutcome.VERIFIED
        and preflight.account_identity_verified
        and preflight.capability_status is HubSpotCapabilityStatus.UNVERIFIED
    ):
        return None
    if preflight.outcome is HubSpotPreflightOutcome.PROVIDER_UNAVAILABLE:
        return _result(
            BookingOutcome.FAILED,
            error_code=BookingOutcomeErrorCode.PROVIDER_UNAVAILABLE,
            next_action=BookingOutcomeNextAction.RETRY_SAME_KEY,
        )
    if preflight.outcome is HubSpotPreflightOutcome.PERMISSION_DENIED:
        code = BookingOutcomeErrorCode.PERMISSION_DENIED
    elif preflight.outcome is HubSpotPreflightOutcome.ACCOUNT_MISMATCH:
        code = BookingOutcomeErrorCode.ACCOUNT_MISMATCH
    elif preflight.outcome is HubSpotPreflightOutcome.INVALID_RESPONSE:
        code = BookingOutcomeErrorCode.INVALID_PROVIDER_RESPONSE
    else:
        code = BookingOutcomeErrorCode.ACTIVATION_REQUIRED
    return _result(
        BookingOutcome.NOT_CONFIGURED,
        error_code=code,
        next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
    )


def _prewrite_provider_failure(error: HubSpotProviderError) -> BookingOutcomeResult:
    if error.code in {
        HubSpotErrorCode.NOT_CONFIGURED,
        HubSpotErrorCode.LIVE_TRANSPORT_DISABLED,
        HubSpotErrorCode.ACTIVATION_REQUIRED,
    }:
        return _result(
            BookingOutcome.NOT_CONFIGURED,
            error_code=BookingOutcomeErrorCode.ACTIVATION_REQUIRED,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )
    if error.code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}:
        return _result(
            BookingOutcome.NOT_CONFIGURED,
            error_code=BookingOutcomeErrorCode.PERMISSION_DENIED,
            next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
        )
    if error.code is HubSpotErrorCode.NOT_FOUND:
        return _no_verified_contact()
    return _result(
        BookingOutcome.FAILED,
        error_code=_provider_error_code(error.code),
        next_action=(
            BookingOutcomeNextAction.RETRY_SAME_KEY
            if error.code
            in {
                HubSpotErrorCode.TIMEOUT,
                HubSpotErrorCode.PROVIDER_UNAVAILABLE,
                HubSpotErrorCode.RATE_LIMITED,
            }
            else BookingOutcomeNextAction.OPERATOR_REVIEW
        ),
    )


def _post_provider_failure(error: HubSpotProviderError) -> BookingOutcomeResult:
    if error.outcome_uncertain:
        return _result(
            BookingOutcome.UNCERTAIN,
            error_code=_provider_error_code(error.code),
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    retryable_no_write = error.code is HubSpotErrorCode.RATE_LIMITED
    return _result(
        BookingOutcome.FAILED,
        error_code=_provider_error_code(error.code),
        next_action=(
            BookingOutcomeNextAction.RETRY_SAME_KEY
            if retryable_no_write
            else BookingOutcomeNextAction.OPERATOR_REVIEW
        ),
    )


def _provider_error_code(code: HubSpotErrorCode) -> BookingOutcomeErrorCode:
    if code in {HubSpotErrorCode.UNAUTHORIZED, HubSpotErrorCode.PERMISSION_DENIED}:
        return BookingOutcomeErrorCode.PERMISSION_DENIED
    if code is HubSpotErrorCode.VALIDATION:
        return BookingOutcomeErrorCode.PROVIDER_VALIDATION
    if code in {HubSpotErrorCode.INVALID_RESPONSE, HubSpotErrorCode.RESPONSE_TOO_LARGE}:
        return BookingOutcomeErrorCode.INVALID_PROVIDER_RESPONSE
    return BookingOutcomeErrorCode.PROVIDER_UNAVAILABLE


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


def _settings_unchanged(
    settings: Settings,
    expected_key: object,
    expected_portal: str | None,
) -> bool:
    return (
        settings.hubspot_service_key is expected_key
        and settings.hubspot_portal_id == expected_portal
        and _settings_configured(settings)
    )


async def _persist_claim_result(
    session_factory: async_sessionmaker[AsyncSession],
    claim: ToolExecution,
    evidence: BookingEvidence,
    result: BookingOutcomeResult,
) -> BookingOutcomeResult:
    try:
        async with session_factory.begin() as session:
            execution = await session.scalar(
                select(ToolExecution).where(ToolExecution.id == claim.id).with_for_update()
            )
            if execution is None or execution.status is not ToolExecutionStatus.STARTED:
                raise SQLAlchemyError("claim state unavailable")
            _terminalize_claim(session, execution, result)
            session.add(_audit_event(evidence, result.outcome.value, result=result))
    except SQLAlchemyError:
        if result.outcome is BookingOutcome.WRITTEN:
            return _result(
                BookingOutcome.UNCERTAIN,
                note_id=result.hubspot_note_id,
                error_code=BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE,
                next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
            )
        if result.outcome is BookingOutcome.UNCERTAIN:
            return result
        return _result(
            BookingOutcome.UNCERTAIN,
            note_id=result.hubspot_note_id,
            error_code=BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE,
            next_action=BookingOutcomeNextAction.RECONCILE_SAME_KEY,
        )
    return result


def _terminalize_claim(
    session: AsyncSession,
    execution: ToolExecution,
    result: BookingOutcomeResult,
) -> None:
    execution.sanitized_result = result.model_dump(mode="json")
    execution.safe_error_code = None if result.error_code is None else result.error_code.value
    execution.safe_error_message = (
        None if result.error_code is None else _safe_error_message(result.error_code)
    )
    if result.outcome is BookingOutcome.WRITTEN:
        execution.status = ToolExecutionStatus.SUCCEEDED
        execution.finished_at = datetime.now(UTC)
    elif result.outcome is BookingOutcome.UNCERTAIN:
        execution.status = ToolExecutionStatus.STARTED
        execution.finished_at = None
    else:
        execution.status = ToolExecutionStatus.FAILED
        execution.finished_at = datetime.now(UTC)


def _safe_error_message(code: BookingOutcomeErrorCode) -> str:
    if code is BookingOutcomeErrorCode.NOTE_IDENTITY_UNVERIFIED:
        return "CRM booking Note creation could not be verified"
    if code is BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE:
        return "CRM booking Note state could not be saved"
    if code is BookingOutcomeErrorCode.PROVIDER_VALIDATION:
        return "HubSpot rejected the structured booking Note"
    return "CRM booking outcome operation requires review"


def _audit_event(
    evidence: BookingEvidence,
    outcome: str,
    *,
    result: BookingOutcomeResult | None = None,
) -> AuditEvent:
    metadata: dict[str, object] = {
        "source_tool_execution_id": str(evidence.source_tool_execution_id),
        "operation_type": _OPERATION,
        "historical_action": evidence.action.value,
        "historical_status": evidence.historical_status.value,
        "outcome": outcome,
    }
    if result is not None:
        if result.error_code is not None:
            metadata["error_code"] = result.error_code.value
        if result.hubspot_note_id is not None:
            metadata["hubspot_note_id"] = result.hubspot_note_id
    return AuditEvent(
        event_type=_OPERATION,
        actor_type=AuditActorType.SYSTEM,
        contact_id=evidence.contact_id,
        conversation_id=evidence.conversation_id,
        entity_type="booking",
        entity_identifier=str(evidence.booking_id),
        sanitized_metadata=metadata,
    )


def _local_persistence_failure() -> BookingOutcomeResult:
    return _result(
        BookingOutcome.FAILED,
        error_code=BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE,
        next_action=BookingOutcomeNextAction.RETRY_SAME_KEY,
    )


def _no_verified_contact() -> BookingOutcomeResult:
    return _result(
        BookingOutcome.SKIPPED_NO_VERIFIED_CONTACT,
        error_code=BookingOutcomeErrorCode.NO_VERIFIED_CONTACT,
        next_action=BookingOutcomeNextAction.OPERATOR_REVIEW,
    )

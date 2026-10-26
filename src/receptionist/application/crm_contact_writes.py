"""Internal, non-mutating contracts for trusted CRM Contact writes.

The request models validate shape, not provenance or caller authority. Their
``trusted_verified_*`` values may only come from a controlled internal path
that independently established the values. This module provides no customer
confirmation mechanism and is not exposed as an HTTP or LLM tool.

A later provider-write implementation must preserve HubSpotClient's existing
account and capability activation guard; this contract adds no test bypass.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist.db.models import Contact, Conversation
from receptionist.domain.identity import PhoneNormalizationError, normalize_phone_number

_EMAIL_LIMIT = 254


class _ContactWriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class _ContactWriteRequest(_ContactWriteModel):
    conversation_id: UUID
    contact_id: UUID
    idempotency_key: str = Field(min_length=36, max_length=36)
    trusted_verified_email: str | None = Field(
        default=None,
        max_length=_EMAIL_LIMIT,
        repr=False,
    )
    trusted_verified_phone: str | None = Field(default=None, max_length=32, repr=False)

    @field_validator("idempotency_key")
    @classmethod
    def validate_canonical_uuid4_key(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("idempotency key must be a canonical UUIDv4") from None
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("idempotency key must be a canonical UUIDv4")
        return value

    @field_validator("trusted_verified_email")
    @classmethod
    def validate_trusted_email(cls, value: str | None) -> str | None:
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
            raise ValueError("trusted verified email is invalid")
        return normalized

    @field_validator("trusted_verified_phone")
    @classmethod
    def validate_trusted_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            canonical = normalize_phone_number(value, None)
        except PhoneNormalizationError:
            raise ValueError("trusted phone must be canonical E.164") from None
        if canonical != value:
            raise ValueError("trusted phone must be canonical E.164")
        return canonical


class CRMContactCreateRequest(_ContactWriteRequest):
    """Shape-checked internal create input; it does not prove trusted provenance."""

    operation: ClassVar[Literal["create_crm_contact"]] = "create_crm_contact"
    trusted_verified_email: str = Field(max_length=_EMAIL_LIMIT, repr=False)


class CRMContactUpdateRequest(_ContactWriteRequest):
    """Shape-checked internal update input; it does not prove trusted provenance."""

    operation: ClassVar[Literal["update_crm_contact"]] = "update_crm_contact"

    @model_validator(mode="after")
    def require_selected_verified_value(self) -> CRMContactUpdateRequest:
        if self.trusted_verified_email is None and self.trusted_verified_phone is None:
            raise ValueError("at least one trusted verified value must be selected")
        return self


class CRMContactCreateOutcome(StrEnum):
    CREATED = "created"
    REPLAYED = "replayed"
    IDENTITY_CONFLICT = "identity_conflict"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    INVALID_REQUEST = "invalid_request"
    OWNERSHIP_REJECTED = "ownership_rejected"


class CRMContactUpdateOutcome(StrEnum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    INVALID_REQUEST = "invalid_request"
    OWNERSHIP_REJECTED = "ownership_rejected"


class CRMContactWriteErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    IDENTITY_CONFLICT = "identity_conflict"
    LOCAL_PERSISTENCE_FAILURE = "local_persistence_failure"


class CRMContactWriteValidationOutcome(StrEnum):
    VALID = "valid"
    INVALID_REQUEST = "invalid_request"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    IDENTITY_CONFLICT = "identity_conflict"
    FAILED = "failed"


class CRMContactWriteNextAction(StrEnum):
    NONE = "none"
    RETRY_SAME_KEY = "retry_same_key"
    RECONCILE_SAME_KEY = "reconcile_same_key"
    OPERATOR_REVIEW = "operator_review"


class CRMContactCreateResult(_ContactWriteModel):
    """Finite safe result contract for a later create implementation."""

    operation: Literal["create_crm_contact"] = "create_crm_contact"
    outcome: CRMContactCreateOutcome
    replayed: bool = False
    hubspot_contact_id: str | None = Field(default=None, min_length=1, max_length=255)
    error_code: CRMContactWriteErrorCode | None = None
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE

    @model_validator(mode="after")
    def replay_outcome_requires_replayed_flag(self) -> CRMContactCreateResult:
        if self.outcome is CRMContactCreateOutcome.REPLAYED and not self.replayed:
            raise ValueError("replayed outcome requires replayed flag")
        return self


class CRMContactUpdateResult(_ContactWriteModel):
    """Finite safe result contract for a later update implementation."""

    operation: Literal["update_crm_contact"] = "update_crm_contact"
    outcome: CRMContactUpdateOutcome
    replayed: bool = False
    updated_fields: tuple[Literal["email", "phone"], ...] = ()
    error_code: CRMContactWriteErrorCode | None = None
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE


class CRMContactWriteValidationResult(_ContactWriteModel):
    """Safe validation result; ``valid`` does not authorize a provider mutation."""

    operation: Literal["create_crm_contact", "update_crm_contact"]
    outcome: CRMContactWriteValidationOutcome
    replayed: bool = False
    error_code: CRMContactWriteErrorCode | None = None
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE


CRMContactWriteRequest = CRMContactCreateRequest | CRMContactUpdateRequest


async def validate_crm_contact_write(
    session_factory: async_sessionmaker[AsyncSession],
    request: CRMContactWriteRequest,
) -> CRMContactWriteValidationResult:
    """Check local ownership and consistency without authenticating or mutating.

    The caller remains responsible for establishing trusted provenance. This
    function deliberately performs no HubSpot request and writes no local state.
    """
    try:
        async with session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == request.conversation_id)
            )
            contact = await session.scalar(select(Contact).where(Contact.id == request.contact_id))
    except SQLAlchemyError:
        return _validation_result(
            request,
            CRMContactWriteValidationOutcome.FAILED,
            error_code=CRMContactWriteErrorCode.LOCAL_PERSISTENCE_FAILURE,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )

    if conversation is None or contact is None:
        return _validation_result(
            request,
            CRMContactWriteValidationOutcome.INVALID_REQUEST,
            error_code=CRMContactWriteErrorCode.INVALID_REQUEST,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if conversation.contact_id != contact.id:
        return _validation_result(
            request,
            CRMContactWriteValidationOutcome.OWNERSHIP_MISMATCH,
            error_code=CRMContactWriteErrorCode.OWNERSHIP_MISMATCH,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    if _identity_conflicts(contact, request):
        return _validation_result(
            request,
            CRMContactWriteValidationOutcome.IDENTITY_CONFLICT,
            error_code=CRMContactWriteErrorCode.IDENTITY_CONFLICT,
            next_action=CRMContactWriteNextAction.OPERATOR_REVIEW,
        )
    return _validation_result(request, CRMContactWriteValidationOutcome.VALID)


def _validation_result(
    request: CRMContactWriteRequest,
    outcome: CRMContactWriteValidationOutcome,
    *,
    error_code: CRMContactWriteErrorCode | None = None,
    next_action: CRMContactWriteNextAction = CRMContactWriteNextAction.NONE,
) -> CRMContactWriteValidationResult:
    return CRMContactWriteValidationResult(
        operation=request.operation,
        outcome=outcome,
        error_code=error_code,
        next_action=next_action,
    )


def _identity_conflicts(contact: Contact, request: CRMContactWriteRequest) -> bool:
    trusted_email = request.trusted_verified_email
    if trusted_email is not None and contact.email is not None and contact.email.strip():
        stored_email = _normalize_email(contact.email)
        if stored_email is None or stored_email != trusted_email:
            return True

    trusted_phone = request.trusted_verified_phone
    if trusted_phone is not None and contact.phone_e164 is not None and contact.phone_e164.strip():
        try:
            stored_phone = normalize_phone_number(contact.phone_e164, None)
        except PhoneNormalizationError:
            return True
        if stored_phone != contact.phone_e164 or stored_phone != trusted_phone:
            return True
    return False


def _normalize_email(value: str) -> str | None:
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
        return None
    return normalized

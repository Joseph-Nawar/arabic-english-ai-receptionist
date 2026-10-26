from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from receptionist.application.crm_resolution import (
    CRMContactResolutionOutcome,
    CRMContactResolutionRequest,
    CRMContactResolutionResult,
    CRMResolutionNextAction,
    _trusted_email_hmac_tag,
)

pytestmark = pytest.mark.unit


def test_resolution_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CRMContactResolutionRequest(
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            idempotency_key=str(uuid.uuid4()),
            provider_contact_id="arbitrary-id",
        )


@pytest.mark.parametrize(
    "idempotency_key",
    [
        "966501234567",
        "fixed-key",
        "not-a-uuid",
        "00000000-0000-1000-8000-000000000001",
        uuid.uuid4().hex,
        "",
    ],
)
def test_resolution_request_requires_canonical_uuid_v4_idempotency_key(
    idempotency_key: str,
) -> None:
    with pytest.raises(ValidationError):
        CRMContactResolutionRequest(
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            idempotency_key=idempotency_key,
        )


def test_resolution_request_accepts_canonical_uuid_v4_idempotency_key() -> None:
    key = str(uuid.uuid4())
    request = CRMContactResolutionRequest(
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        idempotency_key=key,
    )

    assert request.idempotency_key == key


def test_resolution_request_accepts_only_a_bounded_trusted_email_value() -> None:
    request = CRMContactResolutionRequest(
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()),
        trusted_verified_email=" Operator@Example.Test ",
    )

    assert request.trusted_verified_email == "operator@example.test"


@pytest.mark.parametrize("email", ["", "not-an-email", "a@@example.test", "a @example.test"])
def test_resolution_request_rejects_invalid_trusted_email_before_claim(email: str) -> None:
    with pytest.raises(ValidationError):
        CRMContactResolutionRequest(
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            idempotency_key=str(uuid.uuid4()),
            trusted_verified_email=email,
        )


def test_trusted_email_hmac_tag_is_stable_and_binds_normalized_email() -> None:
    key = str(uuid.uuid4())
    first_tag = _trusted_email_hmac_tag(key, " Customer.A@Example.test ")
    normalized_tag = _trusted_email_hmac_tag(key, "customer.a@example.test")
    different_email_tag = _trusted_email_hmac_tag(key, "customer.b@example.test")

    assert first_tag is not None
    assert len(first_tag) == 64
    assert first_tag == normalized_tag
    assert first_tag != different_email_tag
    assert "customer.a@example.test" not in first_tag
    assert key not in first_tag


def test_resolution_result_is_finite_and_contains_only_safe_fields() -> None:
    result = CRMContactResolutionResult(
        outcome=CRMContactResolutionOutcome.FOUND_AND_LINKED,
        hubspot_contact_id="901",
        replayed=False,
        next_action=CRMResolutionNextAction.NONE,
    )

    assert result.operation == "resolve_crm_contact"
    assert "email" not in result.model_dump(mode="json")
    assert "phone" not in result.model_dump(mode="json")

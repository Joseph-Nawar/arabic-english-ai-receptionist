from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from receptionist.application.crm_resolution import (
    CRMContactResolutionOutcome,
    CRMContactResolutionRequest,
    CRMContactResolutionResult,
    CRMResolutionNextAction,
)

pytestmark = pytest.mark.unit


def test_resolution_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CRMContactResolutionRequest(
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            idempotency_key="resolve-key",
            provider_contact_id="arbitrary-id",
        )


@pytest.mark.parametrize("idempotency_key", ["", "customer@example.com", "has spaces", "x" * 129])
def test_resolution_request_requires_bounded_opaque_idempotency_key(
    idempotency_key: str,
) -> None:
    with pytest.raises(ValidationError):
        CRMContactResolutionRequest(
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            idempotency_key=idempotency_key,
        )


def test_resolution_request_accepts_only_a_bounded_trusted_email_value() -> None:
    request = CRMContactResolutionRequest(
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        idempotency_key="resolve-key_1",
        trusted_verified_email=" operator@example.test ",
    )

    assert request.trusted_verified_email == "operator@example.test"


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

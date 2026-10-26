from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from receptionist.application.crm_contact_writes import (
    CRMContactCreateOutcome,
    CRMContactCreateRequest,
    CRMContactCreateResult,
    CRMContactUpdateOutcome,
    CRMContactUpdateRequest,
    CRMContactUpdateResult,
)

pytestmark = pytest.mark.unit


def _ids() -> dict[str, object]:
    return {
        "conversation_id": uuid.uuid4(),
        "contact_id": uuid.uuid4(),
        "idempotency_key": str(uuid.uuid4()),
    }


def test_create_request_accepts_required_trusted_email_and_optional_canonical_phone() -> None:
    request = CRMContactCreateRequest(
        **_ids(),
        trusted_verified_email=" Customer.A@Example.Test ",
        trusted_verified_phone="+966501234567",
    )

    assert request.trusted_verified_email == "customer.a@example.test"
    assert request.trusted_verified_phone == "+966501234567"
    assert request.operation == "create_crm_contact"


def test_create_request_accepts_verified_email_without_a_phone() -> None:
    request = CRMContactCreateRequest(
        **_ids(),
        trusted_verified_email="customer@example.test",
    )

    assert request.trusted_verified_email == "customer@example.test"
    assert request.trusted_verified_phone is None


def test_create_request_requires_trusted_email() -> None:
    with pytest.raises(ValidationError):
        CRMContactCreateRequest(**_ids())


def test_phone_only_create_request_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CRMContactCreateRequest(**_ids(), trusted_verified_phone="+966501234567")


@pytest.mark.parametrize("email", ["", "not-an-email", "a@@example.test", "a @example.test"])
def test_invalid_create_email_is_rejected_without_echoing_value(email: str) -> None:
    sentinel = "private-invalid-email-sentinel"
    supplied = sentinel if email == "not-an-email" else email
    with pytest.raises(ValidationError) as raised:
        CRMContactCreateRequest(**_ids(), trusted_verified_email=supplied)

    assert not supplied or supplied not in str(raised.value)
    assert not email or email not in str(raised.value)


@pytest.mark.parametrize("phone", ["966501234567", "+966 50 123 4567", "+123", "not-a-phone"])
def test_create_phone_must_be_valid_canonical_e164(phone: str) -> None:
    with pytest.raises(ValidationError) as raised:
        CRMContactCreateRequest(
            **_ids(),
            trusted_verified_email="customer@example.test",
            trusted_verified_phone=phone,
        )

    assert phone not in str(raised.value)


def test_requests_reject_unknown_provider_fields_and_confirmation_claims() -> None:
    forbidden_fields = (
        {"properties": {"email": "customer@example.test"}},
        {"hubspot_contact_id": "123"},
        {"provider_contact_id": "123"},
        {"portal_id": "123456"},
        {"hubspot_portal_id": "123456"},
        {"account_id": "123456"},
        {"email_identity_confirmed": True},
        {"verified": True},
    )
    for extra in forbidden_fields:
        with pytest.raises(ValidationError):
            CRMContactCreateRequest(
                **_ids(),
                trusted_verified_email="customer@example.test",
                **extra,
            )


def test_update_request_accepts_email_only_phone_only_or_both() -> None:
    shared = _ids()
    email_only = CRMContactUpdateRequest(
        **shared,
        trusted_verified_email="customer@example.test",
    )
    phone_only = CRMContactUpdateRequest(
        **_ids(),
        trusted_verified_phone="+966501234567",
    )
    both = CRMContactUpdateRequest(
        **_ids(),
        trusted_verified_email="customer@example.test",
        trusted_verified_phone="+966501234567",
    )

    assert email_only.trusted_verified_email == "customer@example.test"
    assert email_only.trusted_verified_phone is None
    assert phone_only.trusted_verified_email is None
    assert phone_only.trusted_verified_phone == "+966501234567"
    assert both.trusted_verified_email == "customer@example.test"
    assert both.trusted_verified_phone == "+966501234567"


def test_empty_update_request_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CRMContactUpdateRequest(**_ids())


def test_write_requests_require_canonical_uuid4_keys() -> None:
    for key in (
        "fixed-key",
        "966501234567",
        "00000000-0000-1000-8000-000000000001",
        uuid.uuid4().hex,
    ):
        with pytest.raises(ValidationError):
            CRMContactCreateRequest(
                **{**_ids(), "idempotency_key": key},
                trusted_verified_email="customer@example.test",
            )


def test_write_request_repr_hides_email_and_phone() -> None:
    email = "write-contract-private@example.test"
    phone = "+966501234567"
    request = CRMContactCreateRequest(
        **_ids(),
        trusted_verified_email=email,
        trusted_verified_phone=phone,
    )

    assert email not in repr(request)
    assert phone not in repr(request)


def test_write_results_serialize_only_finite_safe_fields() -> None:
    email = "write-result-private@example.test"
    phone = "+966501234567"
    create_result = CRMContactCreateResult(
        outcome=CRMContactCreateOutcome.CREATED,
        hubspot_contact_id="opaque-contact-123",
    )
    update_result = CRMContactUpdateResult(
        outcome=CRMContactUpdateOutcome.UPDATED,
        updated_fields=("email", "phone"),
    )

    serialized = f"{create_result.model_dump(mode='json')} {update_result.model_dump(mode='json')}"
    assert email not in serialized
    assert phone not in serialized
    assert "opaque-contact-123" in serialized
    assert "properties" not in serialized


@pytest.mark.parametrize("outcome", list(CRMContactCreateOutcome))
def test_create_result_accepts_only_declared_finite_outcomes(
    outcome: CRMContactCreateOutcome,
) -> None:
    result = CRMContactCreateResult(
        outcome=outcome,
        replayed=outcome is CRMContactCreateOutcome.REPLAYED,
    )
    assert result.outcome is outcome


def test_create_replay_result_requires_replayed_flag() -> None:
    with pytest.raises(ValidationError):
        CRMContactCreateResult(outcome=CRMContactCreateOutcome.REPLAYED)


@pytest.mark.parametrize("outcome", list(CRMContactUpdateOutcome))
def test_update_result_accepts_only_declared_finite_outcomes(
    outcome: CRMContactUpdateOutcome,
) -> None:
    result = CRMContactUpdateResult(outcome=outcome)
    assert result.outcome is outcome

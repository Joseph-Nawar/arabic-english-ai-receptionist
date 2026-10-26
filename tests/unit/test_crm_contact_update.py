from __future__ import annotations

import pytest
from pydantic import ValidationError

from receptionist.application.crm_contact_writes import (
    CRMContactUpdateRequest,
)
from receptionist.integrations.hubspot import HubSpotContactSnapshot

pytestmark = pytest.mark.unit


def _plan(request: CRMContactUpdateRequest, snapshot: HubSpotContactSnapshot):
    try:
        from receptionist.application.crm_contact_update import _plan_update_fields
    except ImportError:
        pytest.fail("Task 10 update field planner has not been implemented")
    return _plan_update_fields(request, snapshot, default_region="SA")


def _request(*, email: str | None = None, phone: str | None = None) -> CRMContactUpdateRequest:
    import uuid

    return CRMContactUpdateRequest(
        contact_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()),
        trusted_verified_email=email,
        trusted_verified_phone=phone,
    )


def _snapshot(
    *,
    email: str | None = None,
    phone: str | None = None,
    mobilephone: str | None = None,
) -> HubSpotContactSnapshot:
    return HubSpotContactSnapshot(
        id="123456",
        email=email,
        phone=phone,
        mobilephone=mobilephone,
        archived=False,
    )


def test_email_field_is_filled_only_when_provider_value_is_blank() -> None:
    request = _request(email="trusted@example.test")

    plan = _plan(request, _snapshot())

    assert plan.patch_email == "trusted@example.test"
    assert plan.patch_phone is None
    assert plan.conflict is False


def test_equal_email_is_unchanged() -> None:
    request = _request(email="trusted@example.test")

    plan = _plan(request, _snapshot(email=" TRUSTED@example.test "))

    assert plan.patch_email is None
    assert plan.patch_phone is None
    assert plan.conflict is False


def test_different_nonblank_email_conflicts_without_patch() -> None:
    request = _request(email="trusted@example.test")

    plan = _plan(request, _snapshot(email="staff-edit@example.test"))

    assert plan.conflict is True
    assert plan.patch_email is None
    assert plan.patch_phone is None


def test_phone_field_is_filled_only_when_primary_provider_phone_is_blank() -> None:
    request = _request(phone="+966501234567")

    plan = _plan(request, _snapshot())

    assert plan.patch_phone == "+966501234567"
    assert plan.patch_email is None
    assert plan.conflict is False


def test_equal_national_format_phone_is_unchanged() -> None:
    request = _request(phone="+966501234567")

    plan = _plan(request, _snapshot(phone="0501234567"))

    assert plan.patch_phone is None
    assert plan.conflict is False


def test_different_nonblank_primary_phone_conflicts_even_if_mobile_matches() -> None:
    request = _request(phone="+966501234567")

    plan = _plan(
        request,
        _snapshot(phone="+966551234567", mobilephone="+966501234567"),
    )

    assert plan.conflict is True
    assert plan.patch_phone is None


def test_both_blank_selected_fields_are_filled() -> None:
    request = _request(email="trusted@example.test", phone="+966501234567")

    plan = _plan(request, _snapshot())

    assert plan.patch_email == "trusted@example.test"
    assert plan.patch_phone == "+966501234567"
    assert plan.conflict is False


def test_both_equal_selected_fields_require_no_patch() -> None:
    request = _request(email="trusted@example.test", phone="+966501234567")

    plan = _plan(request, _snapshot(email="trusted@example.test", phone="+966501234567"))

    assert plan.patch_email is None
    assert plan.patch_phone is None
    assert plan.conflict is False


def test_one_equal_field_and_one_blank_field_patches_only_the_blank_one() -> None:
    request = _request(email="trusted@example.test", phone="+966501234567")

    plan = _plan(request, _snapshot(email="trusted@example.test"))

    assert plan.patch_email is None
    assert plan.patch_phone == "+966501234567"
    assert plan.conflict is False


def test_any_selected_conflict_blocks_partial_update() -> None:
    request = _request(email="trusted@example.test", phone="+966501234567")

    plan = _plan(request, _snapshot(phone="+966551234567"))

    assert plan.conflict is True
    assert plan.patch_email is None
    assert plan.patch_phone is None


def test_email_only_update_never_selects_phone() -> None:
    request = _request(email="trusted@example.test")

    plan = _plan(request, _snapshot(phone="+966551234567", mobilephone="+966501234567"))

    assert plan.patch_email == "trusted@example.test"
    assert plan.patch_phone is None


def test_phone_only_update_never_selects_email() -> None:
    request = _request(phone="+966501234567")

    plan = _plan(request, _snapshot(email="staff@example.test"))

    assert plan.patch_email is None
    assert plan.patch_phone == "+966501234567"


def test_invalid_provider_phone_fails_closed() -> None:
    request = _request(phone="+966501234567")

    plan = _plan(request, _snapshot(phone="not-a-phone"))

    assert plan.conflict is True
    assert plan.patch_phone is None


def test_update_request_rejects_unknown_or_provider_owned_fields() -> None:
    import uuid

    fields = {
        "contact_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "idempotency_key": str(uuid.uuid4()),
        "trusted_verified_email": "trusted@example.test",
    }
    for extra in (
        {"properties": {"ownerId": "99"}},
        {"hubspot_contact_id": "123456"},
        {"portal_id": "123456"},
        {"verified": True},
    ):
        with pytest.raises(ValidationError):
            CRMContactUpdateRequest(**fields, **extra)

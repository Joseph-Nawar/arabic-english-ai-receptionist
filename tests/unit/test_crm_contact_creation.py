from __future__ import annotations

import uuid

import pytest

from receptionist.application.crm_contact_creation import _trusted_evidence_tags
from receptionist.application.crm_contact_writes import (
    CRMContactCreateOutcome,
    CRMContactCreateResult,
)

pytestmark = pytest.mark.unit


def test_existing_provider_contact_has_a_finite_non_create_result() -> None:
    result = CRMContactCreateResult(
        outcome=CRMContactCreateOutcome.EXISTING_CONTACT,
        hubspot_contact_id="123456789",
    )

    assert result.outcome is CRMContactCreateOutcome.EXISTING_CONTACT
    assert result.hubspot_contact_id == "123456789"
    assert result.replayed is False


def test_trusted_evidence_tags_are_stable_and_keyed_to_selected_values() -> None:
    key = str(uuid.uuid4())

    first = _trusted_evidence_tags(
        key,
        email="Customer@example.test",
        phone="+966501234567",
    )
    same_normalized_values = _trusted_evidence_tags(
        key,
        email=" customer@EXAMPLE.test ",
        phone="+966501234567",
    )
    changed_email = _trusted_evidence_tags(
        key,
        email="other@example.test",
        phone="+966501234567",
    )
    changed_phone = _trusted_evidence_tags(
        key,
        email="customer@example.test",
        phone="+966551234567",
    )

    assert first == same_normalized_values
    assert first[0] != changed_email[0]
    assert first[1] != changed_phone[1]
    assert all(tag is None or len(tag) == 64 for tag in first)

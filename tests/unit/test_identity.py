from __future__ import annotations

import pytest
from pydantic import ValidationError

from receptionist.domain.config import HandoffPolicySpec
from receptionist.domain.identity import PhoneNormalizationError, normalize_phone_number

pytestmark = pytest.mark.unit


def test_international_phone_normalizes_to_e164() -> None:
    assert normalize_phone_number("+966 50 123 4567", None) == "+966501234567"


def test_national_saudi_phone_requires_region_and_normalizes_to_same_e164() -> None:
    assert normalize_phone_number("0501234567", "SA") == "+966501234567"


@pytest.mark.parametrize(
    ("raw_number", "default_region"),
    [
        ("0501234567", None),
        ("0501234567", "ZZ"),
        ("not-a-number", "SA"),
        ("+999123456789", None),
        ("0501234567 ext 89", "SA"),
        ("", "SA"),
    ],
)
def test_invalid_phone_values_are_rejected(raw_number: str, default_region: str | None) -> None:
    with pytest.raises(PhoneNormalizationError):
        normalize_phone_number(raw_number, default_region)


def test_handoff_phone_is_canonicalized_as_international_e164() -> None:
    policy = HandoffPolicySpec(
        enabled=True,
        handoff_phone_e164="+966 50 123 4567",
        business_hours_only=True,
    )

    assert policy.handoff_phone_e164 == "+966501234567"


def test_handoff_phone_rejects_national_input_without_region() -> None:
    with pytest.raises(ValidationError, match="handoff_phone_e164"):
        HandoffPolicySpec(
            enabled=True,
            handoff_phone_e164="0501234567",
            business_hours_only=True,
        )


def test_libphonenumber_supported_00_prefix_is_used_without_custom_parsing() -> None:
    try:
        normalized = normalize_phone_number("0012025550100", "SA")
    except PhoneNormalizationError:
        pytest.skip("libphonenumber does not support this 00-prefixed input")

    assert normalized == "+12025550100"

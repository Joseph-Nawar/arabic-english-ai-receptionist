"""Pure canonical phone identity normalization."""

from __future__ import annotations

import phonenumbers
from phonenumbers import NumberParseException


class PhoneNormalizationError(ValueError):
    """Raised when a phone value cannot be used as a canonical identity."""


def normalize_phone_number(raw_number: str, default_region: str | None) -> str:
    """Parse and return a valid, possible phone number in E.164 format."""
    value = raw_number.strip()
    if not value:
        raise PhoneNormalizationError("phone number cannot be blank")

    region: str | None
    if value.startswith("+"):
        region = None
    else:
        if default_region is None:
            raise PhoneNormalizationError("national phone numbers require a default region")
        region = default_region.upper()
        if region not in phonenumbers.SUPPORTED_REGIONS:
            raise PhoneNormalizationError("default region is not supported")

    try:
        parsed = phonenumbers.parse(value, region)
    except NumberParseException as exc:
        raise PhoneNormalizationError("phone number could not be parsed") from exc

    if parsed.extension:
        raise PhoneNormalizationError("phone extensions are not valid identity values")
    if not phonenumbers.is_possible_number(parsed):
        raise PhoneNormalizationError("phone number is not possible")
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneNormalizationError("phone number is not valid")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

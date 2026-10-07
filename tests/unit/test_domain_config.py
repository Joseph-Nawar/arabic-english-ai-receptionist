from __future__ import annotations

from datetime import time

import pytest
from pydantic import ValidationError

from receptionist.domain.config import (
    AfterHoursPolicySpec,
    BilingualText,
    BookingPolicySpec,
    BusinessConfigSpec,
    HandoffPolicySpec,
    PricingSpec,
    ServiceAreaSpec,
    ServiceEscalationSpec,
    ServiceSpec,
    TimeWindow,
    WeeklyHours,
    normalize_catalog_text,
    validate_service_areas,
    validate_service_catalog,
)
from receptionist.domain.enums import AfterHoursMode, BusinessLanguage, PricingMode, Weekday

pytestmark = pytest.mark.unit


def _business_values() -> dict[str, object]:
    return {
        "business_name": "Synthetic HomeCare Demo",
        "timezone": "Asia/Riyadh",
        "default_phone_region": "SA",
        "default_currency": "SAR",
        "supported_languages": [BusinessLanguage.ENGLISH, BusinessLanguage.ARABIC],
        "default_language": BusinessLanguage.ENGLISH,
        "weekly_hours": WeeklyHours(
            days={
                Weekday.SUNDAY: [TimeWindow(start=time(8), end=time(20))],
                Weekday.FRIDAY: [],
            }
        ),
        "after_hours_policy": AfterHoursPolicySpec(mode=AfterHoursMode.CLOSED_MESSAGE),
        "service_areas": [
            ServiceAreaSpec(
                code="al_olaya",
                name_en="Al Olaya",
                name_ar="العليا",
                aliases=["Olaya"],
                active=True,
            )
        ],
        "booking_policy": BookingPolicySpec(
            minimum_notice_minutes=30,
            maximum_advance_days=30,
            buffer_before_minutes=10,
            buffer_after_minutes=15,
            slot_increment_minutes=30,
        ),
        "handoff_policy": HandoffPolicySpec(
            enabled=True,
            handoff_phone_e164=None,
            business_hours_only=True,
        ),
        "greeting_templates": BilingualText(en="Hello", ar="مرحبًا"),
        "closing_templates": BilingualText(en="Goodbye", ar="مع السلامة"),
        "retention_policy": {
            "transcript_retention_days": 30,
            "audit_retention_days": 90,
            "raw_voice_media_retention_days": 1,
        },
    }


def _service(
    *,
    code: str = "plumbing",
    name_en: str = "Plumbing",
    name_ar: str = "سباكة",
    aliases: list[str] | None = None,
    duration: int = 60,
) -> ServiceSpec:
    return ServiceSpec(
        code=code,
        name_en=name_en,
        name_ar=name_ar,
        aliases=aliases or [],
        description_en="Synthetic service",
        description_ar="خدمة تجريبية",
        active=True,
        bookable=True,
        default_duration_minutes=duration,
        pricing=PricingSpec(mode=PricingMode.NOT_PUBLISHED),
        booking_requirements=[],
        escalation_policy=ServiceEscalationSpec(requires_human=False),
    )


def test_valid_business_config_is_strict_and_json_safe() -> None:
    config = BusinessConfigSpec(**_business_values())

    dumped = config.model_dump(mode="json")

    assert dumped["default_currency"] == "SAR"
    assert dumped["weekly_hours"]["days"]["sunday"][0]["start"] == "08:00:00"
    assert "services" not in dumped


@pytest.mark.parametrize(
    ("field", "value"),
    [("timezone", "Mars/Olympus"), ("default_phone_region", "ZZ"), ("default_currency", "sar")],
)
def test_business_config_rejects_invalid_core_values(field: str, value: str) -> None:
    values = _business_values()
    values[field] = value

    with pytest.raises(ValidationError, match=field):
        BusinessConfigSpec(**values)


def test_default_language_must_be_supported() -> None:
    values = _business_values()
    values["supported_languages"] = [BusinessLanguage.ARABIC]
    values["default_language"] = BusinessLanguage.ENGLISH

    with pytest.raises(ValidationError, match="default_language"):
        BusinessConfigSpec(**values)


def test_unknown_business_config_fields_are_forbidden() -> None:
    values = _business_values()
    values["unexpected"] = True

    with pytest.raises(ValidationError, match="unexpected"):
        BusinessConfigSpec(**values)


def test_weekly_hours_allow_empty_days() -> None:
    hours = WeeklyHours(days={Weekday.FRIDAY: []})

    assert hours.days[Weekday.FRIDAY] == []


@pytest.mark.parametrize(
    "window_values",
    [
        [(time(8), time(12)), (time(11), time(14))],
        [(time(8), time(12)), (time(8), time(12))],
        [(time(12), time(12))],
        [(time(22), time(2))],
    ],
)
def test_weekly_hours_reject_invalid_or_overlapping_windows(
    window_values: list[tuple[time, time]],
) -> None:
    with pytest.raises(ValidationError):
        WeeklyHours(
            days={
                Weekday.SUNDAY: [TimeWindow(start=start, end=end) for start, end in window_values]
            }
        )


def test_service_area_aliases_are_normalized_for_ambiguity() -> None:
    areas = [
        ServiceAreaSpec(
            code="one",
            name_en="Al Olaya",
            name_ar="العليا",
            aliases=["Central"],
            active=True,
        ),
        ServiceAreaSpec(
            code="two",
            name_en="Al Malaz",
            name_ar="الملز",
            aliases=["  CENTRAL  "],
            active=True,
        ),
    ]

    with pytest.raises(ValueError, match="ambiguous"):
        validate_service_areas(areas)


def test_service_area_codes_must_be_unique() -> None:
    areas = [
        ServiceAreaSpec(code="same", name_en="First", name_ar="الأول"),
        ServiceAreaSpec(code="same", name_en="Second", name_ar="الثاني"),
    ]

    with pytest.raises(ValueError, match="codes must be unique"):
        validate_service_areas(areas)


def test_catalog_text_uses_basic_unicode_whitespace_and_case_normalization() -> None:
    assert normalize_catalog_text("  Cafe\u0301  Repair ") == "café repair"


@pytest.mark.parametrize(
    "pricing",
    [
        PricingSpec(mode=PricingMode.FIXED, amount_minor=1000),
        PricingSpec(mode=PricingMode.FROM, minimum_amount_minor=1000),
        PricingSpec(
            mode=PricingMode.RANGE,
            minimum_amount_minor=1000,
            maximum_amount_minor=2500,
        ),
        PricingSpec(mode=PricingMode.QUOTE_REQUIRED),
        PricingSpec(mode=PricingMode.NOT_PUBLISHED),
    ],
)
def test_all_pricing_modes_are_valid(pricing: PricingSpec) -> None:
    assert pricing.mode in PricingMode


@pytest.mark.parametrize(
    "values",
    [
        {"mode": PricingMode.FIXED, "minimum_amount_minor": 1000},
        {"mode": PricingMode.FROM, "amount_minor": 1000},
        {"mode": PricingMode.RANGE, "minimum_amount_minor": 2500, "maximum_amount_minor": 1000},
        {"mode": PricingMode.QUOTE_REQUIRED, "amount_minor": 1000},
        {"mode": PricingMode.NOT_PUBLISHED, "minimum_amount_minor": 1000},
    ],
)
def test_pricing_rejects_invalid_mode_combinations(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PricingSpec(**values)


def test_service_duration_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="default_duration_minutes"):
        _service(duration=0)


def test_service_catalog_rejects_ambiguous_names_and_aliases() -> None:
    services = [
        _service(code="one", name_en="AC Repair"),
        _service(code="two", name_en="Electrical", aliases=[" ac  repair "]),
    ]

    with pytest.raises(ValueError, match="ambiguous"):
        validate_service_catalog(services)

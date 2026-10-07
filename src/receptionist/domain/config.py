"""Strict Pydantic structures for Phase 1 business and service configuration."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from receptionist.domain.enums import (
    AfterHoursMode,
    BusinessLanguage,
    PricingMode,
    Weekday,
)
from receptionist.domain.identity import PhoneNormalizationError, normalize_phone_number

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BilingualText(_StrictModel):
    en: str = Field(min_length=1)
    ar: str = Field(min_length=1)


class TimeWindow(_StrictModel):
    start: time
    end: time

    @model_validator(mode="after")
    def validate_order(self) -> TimeWindow:
        if self.start >= self.end:
            raise ValueError("weekly-hours windows must have start before end")
        return self


class WeeklyHours(_StrictModel):
    days: dict[Weekday, list[TimeWindow]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_windows(self) -> WeeklyHours:
        for day, windows in self.days.items():
            del day
            previous_end: time | None = None
            for window in sorted(windows, key=lambda item: item.start):
                if previous_end is not None and window.start < previous_end:
                    raise ValueError("weekly-hours windows cannot overlap or duplicate")
                previous_end = window.end
        return self


class AfterHoursPolicySpec(_StrictModel):
    mode: AfterHoursMode


class ServiceAreaSpec(_StrictModel):
    code: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    name_ar: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class BookingPolicySpec(_StrictModel):
    minimum_notice_minutes: Annotated[int, Field(ge=0)]
    maximum_advance_days: Annotated[int, Field(gt=0)]
    buffer_before_minutes: Annotated[int, Field(ge=0)]
    buffer_after_minutes: Annotated[int, Field(ge=0)]
    slot_increment_minutes: Annotated[int, Field(gt=0)]


class HandoffPolicySpec(_StrictModel):
    enabled: bool
    handoff_phone_e164: str | None = None
    business_hours_only: bool

    @field_validator("handoff_phone_e164")
    @classmethod
    def validate_handoff_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_phone_number(value, None)
        except PhoneNormalizationError as exc:
            raise ValueError(
                "handoff_phone_e164 must be a valid international E.164 number"
            ) from exc


class RetentionPolicySpec(_StrictModel):
    transcript_retention_days: Annotated[int, Field(ge=0)]
    audit_retention_days: Annotated[int, Field(ge=0)]
    raw_voice_media_retention_days: Annotated[int, Field(ge=0)]


class PricingSpec(_StrictModel):
    mode: PricingMode
    amount_minor: Annotated[int, Field(ge=0)] | None = None
    minimum_amount_minor: Annotated[int, Field(ge=0)] | None = None
    maximum_amount_minor: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_amounts(self) -> PricingSpec:
        amounts = {
            "amount_minor": self.amount_minor,
            "minimum_amount_minor": self.minimum_amount_minor,
            "maximum_amount_minor": self.maximum_amount_minor,
        }
        allowed: dict[PricingMode, set[str]] = {
            PricingMode.FIXED: {"amount_minor"},
            PricingMode.FROM: {"minimum_amount_minor"},
            PricingMode.RANGE: {"minimum_amount_minor", "maximum_amount_minor"},
            PricingMode.QUOTE_REQUIRED: set(),
            PricingMode.NOT_PUBLISHED: set(),
        }
        permitted = allowed[self.mode]
        if any(value is not None and field not in permitted for field, value in amounts.items()):
            raise ValueError(f"pricing amounts are invalid for mode {self.mode.value}")
        if any(amounts[field] is None for field in permitted):
            raise ValueError(f"pricing mode {self.mode.value} requires its amount fields")
        if (
            self.mode is PricingMode.RANGE
            and self.minimum_amount_minor is not None
            and self.maximum_amount_minor is not None
            and self.minimum_amount_minor > self.maximum_amount_minor
        ):
            raise ValueError("range pricing minimum cannot exceed maximum")
        return self


class BookingRequirementSpec(_StrictModel):
    key: str = Field(min_length=1)
    label_en: str = Field(min_length=1)
    label_ar: str = Field(min_length=1)
    required: bool


class ServiceEscalationSpec(_StrictModel):
    requires_human: bool


class BusinessConfigSpec(_StrictModel):
    business_name: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    default_phone_region: str = Field(min_length=2, max_length=2)
    default_currency: str = Field(min_length=3, max_length=3)
    supported_languages: list[BusinessLanguage] = Field(min_length=1)
    default_language: BusinessLanguage
    weekly_hours: WeeklyHours
    after_hours_policy: AfterHoursPolicySpec
    service_areas: list[ServiceAreaSpec] = Field(default_factory=list)
    booking_policy: BookingPolicySpec
    handoff_policy: HandoffPolicySpec
    greeting_templates: BilingualText
    closing_templates: BilingualText
    retention_policy: RetentionPolicySpec

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("default_phone_region")
    @classmethod
    def validate_phone_region(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in phonenumbers.SUPPORTED_REGIONS:
            raise ValueError("default_phone_region must be a supported region code")
        return normalized

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if not _CURRENCY_PATTERN.fullmatch(value):
            raise ValueError("default_currency must be an uppercase three-letter code")
        return value

    @model_validator(mode="after")
    def validate_business_catalog(self) -> BusinessConfigSpec:
        if self.default_language not in self.supported_languages:
            raise ValueError("default_language must be in supported_languages")
        validate_service_areas(self.service_areas)
        return self


class ServiceSpec(_StrictModel):
    code: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    name_ar: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description_en: str = Field(min_length=1)
    description_ar: str = Field(min_length=1)
    active: bool = True
    bookable: bool = True
    default_duration_minutes: Annotated[int, Field(gt=0)]
    pricing: PricingSpec
    booking_requirements: list[BookingRequirementSpec] = Field(default_factory=list)
    escalation_policy: ServiceEscalationSpec


def normalize_catalog_text(value: str) -> str:
    """Apply only deterministic Unicode, whitespace, and case normalization."""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _validate_unique_catalog_labels(
    items: Sequence[ServiceAreaSpec] | Sequence[ServiceSpec], label: str
) -> None:
    seen: dict[str, str] = {}
    for item in items:
        code = item.code
        labels = [item.name_en, item.name_ar, *item.aliases]
        for raw_label in labels:
            normalized = normalize_catalog_text(raw_label)
            if normalized in seen:
                raise ValueError(f"ambiguous {label} label: {raw_label}")
            seen[normalized] = code


def validate_service_areas(areas: Sequence[ServiceAreaSpec]) -> None:
    """Reject duplicate area codes and ambiguous names/aliases."""
    codes = [area.code for area in areas]
    if len(codes) != len(set(codes)):
        raise ValueError("service-area codes must be unique")
    _validate_unique_catalog_labels(areas, "service-area")


def validate_service_catalog(services: Sequence[ServiceSpec]) -> None:
    """Reject duplicate service codes and ambiguous names/aliases."""
    codes = [service.code for service in services]
    if len(codes) != len(set(codes)):
        raise ValueError("service codes must be unique")
    _validate_unique_catalog_labels(services, "service")

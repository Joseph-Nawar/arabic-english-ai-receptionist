"""Synthetic reference business and service seed."""

from __future__ import annotations

import asyncio
import logging
from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from receptionist.core.config import Settings, get_settings
from receptionist.db.models import BusinessConfig, Service
from receptionist.db.session import create_database_resources, dispose_database
from receptionist.domain.config import (
    AfterHoursPolicySpec,
    BilingualText,
    BookingPolicySpec,
    BookingRequirementSpec,
    BusinessConfigSpec,
    HandoffPolicySpec,
    PricingSpec,
    RetentionPolicySpec,
    ServiceAreaSpec,
    ServiceEscalationSpec,
    ServiceSpec,
    TimeWindow,
    WeeklyHours,
    validate_service_catalog,
)
from receptionist.domain.enums import AfterHoursMode, BusinessLanguage, PricingMode, Weekday

LOGGER = logging.getLogger("receptionist.seed")
_PERMITTED_ENVIRONMENTS = {"local", "test", "ci"}


def build_reference_business() -> BusinessConfigSpec:
    """Build the validated synthetic business configuration."""
    open_all_day = [TimeWindow(start=time(8), end=time(20))]
    return BusinessConfigSpec(
        business_name="Riyadh HomeCare Demo",
        timezone="Asia/Riyadh",
        default_phone_region="SA",
        default_currency="SAR",
        supported_languages=[BusinessLanguage.ENGLISH, BusinessLanguage.ARABIC],
        default_language=BusinessLanguage.ENGLISH,
        weekly_hours=WeeklyHours(
            days={
                Weekday.SUNDAY: open_all_day,
                Weekday.MONDAY: open_all_day,
                Weekday.TUESDAY: open_all_day,
                Weekday.WEDNESDAY: open_all_day,
                Weekday.THURSDAY: open_all_day,
                Weekday.FRIDAY: [TimeWindow(start=time(14), end=time(20))],
                Weekday.SATURDAY: open_all_day,
            }
        ),
        after_hours_policy=AfterHoursPolicySpec(mode=AfterHoursMode.CLOSED_MESSAGE),
        service_areas=[
            ServiceAreaSpec(
                code="al_olaya",
                name_en="Al Olaya",
                name_ar="العليا",
                aliases=["olaya", "al-olaya"],
            ),
            ServiceAreaSpec(
                code="al_malaz",
                name_en="Al Malaz",
                name_ar="الملز",
                aliases=["malaz", "al-malaz"],
            ),
            ServiceAreaSpec(
                code="al_nakheel",
                name_en="Al Nakheel",
                name_ar="النخيل",
                aliases=["nakheel", "al-nakheel"],
            ),
            ServiceAreaSpec(
                code="al_yasmin",
                name_en="Al Yasmin",
                name_ar="الياسمين",
                aliases=["yasmin", "al-yasmin"],
            ),
        ],
        booking_policy=BookingPolicySpec(
            minimum_notice_minutes=120,
            maximum_advance_days=30,
            buffer_before_minutes=15,
            buffer_after_minutes=15,
            slot_increment_minutes=30,
        ),
        handoff_policy=HandoffPolicySpec(
            enabled=True,
            handoff_phone_e164="+1 202 555 0100",
            business_hours_only=True,
        ),
        greeting_templates=BilingualText(
            en="Welcome to Riyadh HomeCare Demo.",
            ar="مرحباً بكم في تجربة رعاية منزلية الرياض.",
        ),
        closing_templates=BilingualText(
            en="Thank you for contacting Riyadh HomeCare Demo.",
            ar="شكراً لتواصلكم مع تجربة رعاية منزلية الرياض.",
        ),
        retention_policy=RetentionPolicySpec(
            transcript_retention_days=30,
            audit_retention_days=90,
            raw_voice_media_retention_days=0,
        ),
    )


def build_reference_services() -> tuple[ServiceSpec, ...]:
    """Build the four validated synthetic service catalog entries."""
    services = (
        ServiceSpec(
            code="ac_maintenance_repair",
            name_en="Air Conditioning Maintenance",
            name_ar="صيانة وإصلاح التكييف",
            aliases=["ac maintenance", "air conditioning"],
            description_en="Synthetic demo description for air conditioning maintenance.",
            description_ar="وصف تجريبي اصطناعي لصيانة التكييف.",
            default_duration_minutes=90,
            pricing=PricingSpec(mode=PricingMode.NOT_PUBLISHED),
            booking_requirements=[
                BookingRequirementSpec(
                    key="unit_count",
                    label_en="Number of units",
                    label_ar="عدد الوحدات",
                    required=False,
                )
            ],
            escalation_policy=ServiceEscalationSpec(requires_human=False),
        ),
        ServiceSpec(
            code="plumbing",
            name_en="Plumbing",
            name_ar="السباكة",
            aliases=["plumber", "water repair"],
            description_en="Synthetic demo description for plumbing requests.",
            description_ar="وصف تجريبي اصطناعي لطلبات السباكة.",
            default_duration_minutes=60,
            pricing=PricingSpec(mode=PricingMode.NOT_PUBLISHED),
            booking_requirements=[],
            escalation_policy=ServiceEscalationSpec(requires_human=False),
        ),
        ServiceSpec(
            code="electrical",
            name_en="Electrical",
            name_ar="الكهرباء",
            aliases=["electrician", "power repair"],
            description_en="Synthetic demo description for electrical requests.",
            description_ar="وصف تجريبي اصطناعي للطلبات الكهربائية.",
            default_duration_minutes=60,
            pricing=PricingSpec(mode=PricingMode.NOT_PUBLISHED),
            booking_requirements=[],
            escalation_policy=ServiceEscalationSpec(requires_human=True),
        ),
        ServiceSpec(
            code="appliance_general_maintenance",
            name_en="General Appliance Maintenance",
            name_ar="صيانة الأجهزة المنزلية العامة",
            aliases=["appliance repair", "home appliance maintenance"],
            description_en="Synthetic demo description for general appliance maintenance.",
            description_ar="وصف تجريبي اصطناعي لصيانة الأجهزة المنزلية العامة.",
            default_duration_minutes=90,
            pricing=PricingSpec(mode=PricingMode.NOT_PUBLISHED),
            booking_requirements=[],
            escalation_policy=ServiceEscalationSpec(requires_human=False),
        ),
    )
    validate_service_catalog(services)
    return services


def _ensure_permitted_environment(app_env: str) -> None:
    if app_env == "production":
        raise RuntimeError("reference seed is disabled in production")
    if app_env not in _PERMITTED_ENVIRONMENTS:
        raise ValueError(f"unsupported application environment: {app_env}")


async def seed_reference_data(session: AsyncSession, app_env: str) -> None:
    """Validate and persist synthetic reference data without committing."""
    _ensure_permitted_environment(app_env)
    business = build_reference_business()
    services = build_reference_services()

    business_row = await session.scalar(select(BusinessConfig).where(BusinessConfig.id == 1))
    if business_row is None:
        business_row = BusinessConfig(id=1, **business.model_dump(mode="json"))
        session.add(business_row)
        await session.flush()

    for service in services:
        service_row = await session.scalar(select(Service).where(Service.code == service.code))
        if service_row is None:
            session.add(Service(**service.model_dump(mode="json")))
            await session.flush()


async def run_seed(settings: Settings) -> None:
    """Run the reference seed with explicit resource and transaction ownership."""
    _ensure_permitted_environment(settings.app_env)
    resources = create_database_resources(settings)
    try:
        async with resources.session_factory.begin() as session:
            await seed_reference_data(session, settings.app_env)
        LOGGER.info("Reference data transaction committed for business_id=1")
    finally:
        await dispose_database(resources)


def main() -> None:
    """Run the reference seed as a module entrypoint."""
    settings = get_settings()
    asyncio.run(run_seed(settings))


if __name__ == "__main__":
    main()

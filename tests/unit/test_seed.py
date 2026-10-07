from __future__ import annotations

import pytest

from receptionist import seed
from receptionist.core.config import Settings
from receptionist.domain.enums import BusinessLanguage, PricingMode
from receptionist.seed import build_reference_business, build_reference_services

pytestmark = pytest.mark.unit


def test_reference_business_is_valid_and_synthetic() -> None:
    business = build_reference_business()

    assert business.business_name == "Riyadh HomeCare Demo"
    assert business.timezone == "Asia/Riyadh"
    assert business.default_phone_region == "SA"
    assert business.default_currency == "SAR"
    assert business.supported_languages == [BusinessLanguage.ENGLISH, BusinessLanguage.ARABIC]
    assert business.handoff_policy.handoff_phone_e164 == "+12025550100"
    assert all(area.code.startswith("al_") for area in business.service_areas)


def test_reference_services_have_exact_stable_codes_and_noncommercial_pricing() -> None:
    services = build_reference_services()

    assert len(services) == 4
    assert {service.code for service in services} == {
        "ac_maintenance_repair",
        "plumbing",
        "electrical",
        "appliance_general_maintenance",
    }
    assert all(service.pricing.mode is PricingMode.NOT_PUBLISHED for service in services)


@pytest.mark.asyncio
async def test_production_seed_refuses_before_opening_database(monkeypatch) -> None:
    def fail_if_database_is_opened(settings):
        raise AssertionError("production seed opened database resources")

    monkeypatch.setattr(seed, "create_database_resources", fail_if_database_is_opened)
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://localhost/receptionist_test",
    )
    with pytest.raises(RuntimeError, match="production"):
        await seed.run_seed(settings)

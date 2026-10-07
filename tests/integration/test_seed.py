from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select

from receptionist.core.config import assert_safe_test_database
from receptionist.db.models import BusinessConfig, Service
from receptionist.db.session import create_database_resources, dispose_database
from receptionist.seed import build_reference_services, seed_reference_data

pytestmark = pytest.mark.integration

REFERENCE_SERVICE_CODES = (
    "ac_maintenance_repair",
    "plumbing",
    "electrical",
    "appliance_general_maintenance",
)


async def _remove_test_seed_rows(resources, integration_settings, unrelated_code: str) -> None:
    assert_safe_test_database(integration_settings)
    async with resources.session_factory.begin() as session:
        await session.execute(delete(Service).where(Service.code.in_(REFERENCE_SERVICE_CODES)))
        await session.execute(delete(Service).where(Service.code == unrelated_code))
        await session.execute(delete(BusinessConfig).where(BusinessConfig.id == 1))


async def test_reference_seed_is_idempotent(integration_settings) -> None:
    assert_safe_test_database(integration_settings)
    resources = create_database_resources(integration_settings)
    unrelated_code = f"test-only-{uuid.uuid4().hex}"

    try:
        await _remove_test_seed_rows(resources, integration_settings, unrelated_code)

        unrelated_service = build_reference_services()[0].model_dump(mode="json")
        unrelated_service["code"] = unrelated_code
        async with resources.session_factory.begin() as session:
            session.add(Service(**unrelated_service))

        async with resources.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(BusinessConfig)) == 0
            for code in REFERENCE_SERVICE_CODES:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(Service).where(Service.code == code)
                    )
                    == 0
                )

        async with resources.session_factory.begin() as session:
            await seed_reference_data(session, integration_settings.app_env)

        async with resources.session_factory() as session:
            first_business_ids = (await session.scalars(select(BusinessConfig.id))).all()
            first_seed_counts = {
                code: await session.scalar(
                    select(func.count()).select_from(Service).where(Service.code == code)
                )
                for code in REFERENCE_SERVICE_CODES
            }
            assert (
                await session.scalar(select(Service.id).where(Service.code == unrelated_code))
                is not None
            )

        async with resources.session_factory.begin() as session:
            await seed_reference_data(session, integration_settings.app_env)

        async with resources.session_factory() as session:
            second_business_ids = (await session.scalars(select(BusinessConfig.id))).all()
            second_seed_counts = {
                code: await session.scalar(
                    select(func.count()).select_from(Service).where(Service.code == code)
                )
                for code in REFERENCE_SERVICE_CODES
            }
            assert (
                await session.scalar(select(Service.id).where(Service.code == unrelated_code))
                is not None
            )

        assert first_business_ids == second_business_ids == [1]
        assert first_seed_counts == second_seed_counts == dict.fromkeys(REFERENCE_SERVICE_CODES, 1)
    finally:
        await _remove_test_seed_rows(resources, integration_settings, unrelated_code)
        await dispose_database(resources)

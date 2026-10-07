from __future__ import annotations

import pytest
from sqlalchemy import func, select

from receptionist.db.models import BusinessConfig, Service
from receptionist.db.session import create_database_resources, dispose_database
from receptionist.seed import seed_reference_data

pytestmark = pytest.mark.integration


async def test_reference_seed_is_idempotent(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.session_factory.begin() as session:
            await seed_reference_data(session, integration_settings.app_env)

        async with resources.session_factory() as session:
            first_business_count = await session.scalar(
                select(func.count()).select_from(BusinessConfig)
            )
            first_service_count = await session.scalar(select(func.count()).select_from(Service))

        async with resources.session_factory.begin() as session:
            await seed_reference_data(session, integration_settings.app_env)

        async with resources.session_factory() as session:
            second_business_count = await session.scalar(
                select(func.count()).select_from(BusinessConfig)
            )
            second_service_count = await session.scalar(select(func.count()).select_from(Service))

        assert first_business_count == second_business_count == 1
        assert first_service_count == second_service_count == 4
    finally:
        await dispose_database(resources)

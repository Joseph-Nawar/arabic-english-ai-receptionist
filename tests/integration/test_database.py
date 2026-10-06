from __future__ import annotations

import pytest
from sqlalchemy import text

from receptionist.db.session import create_database_resources, dispose_database

pytestmark = pytest.mark.integration


async def test_real_postgresql_connectivity_and_clean_disposal(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await dispose_database(resources)

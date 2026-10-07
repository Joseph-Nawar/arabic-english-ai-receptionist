from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic.config import Config

from alembic import command
from receptionist.core.config import Settings, assert_safe_test_database
from receptionist.db.session import create_database_resources, dispose_database

TEST_DATABASE_URL = (
    "postgresql+psycopg://receptionist:receptionist@localhost:55432/receptionist_test"
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=os.environ.get("RECEPTIONIST_DATABASE_URL", TEST_DATABASE_URL),
    )
    assert_safe_test_database(settings)
    return settings


@pytest.fixture(scope="session", autouse=True)
def ensure_database_at_head(integration_settings: Settings) -> Iterator[None]:
    """Bring only the guarded integration database to the current migration head."""
    assert_safe_test_database(integration_settings)
    environment = {
        "RECEPTIONIST_APP_ENV": os.environ.get("RECEPTIONIST_APP_ENV"),
        "RECEPTIONIST_DATABASE_URL": os.environ.get("RECEPTIONIST_DATABASE_URL"),
    }
    os.environ["RECEPTIONIST_APP_ENV"] = integration_settings.app_env
    os.environ["RECEPTIONIST_DATABASE_URL"] = integration_settings.database_url_value
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield
    finally:
        for key, value in environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
async def session_factory(integration_settings):
    """Provide a function-scoped async session factory for integration records."""
    resources = create_database_resources(integration_settings)
    try:
        yield resources.session_factory
    finally:
        await dispose_database(resources)

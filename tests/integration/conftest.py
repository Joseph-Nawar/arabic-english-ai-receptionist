from __future__ import annotations

import os

import pytest

from receptionist.core.config import Settings, assert_safe_test_database

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

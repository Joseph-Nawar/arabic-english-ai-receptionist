from __future__ import annotations

import os

import pytest
from alembic.config import Config

from alembic import command
from receptionist.core.config import assert_safe_test_database

pytestmark = pytest.mark.integration


def test_alembic_upgrade_downgrade_upgrade_cycle(integration_settings, monkeypatch) -> None:
    assert_safe_test_database(integration_settings)
    database_url = integration_settings.database_url_value
    monkeypatch.setenv("RECEPTIONIST_APP_ENV", integration_settings.app_env)
    monkeypatch.setenv("RECEPTIONIST_DATABASE_URL", database_url)
    monkeypatch.delenv("RECEPTIONIST_LOG_LEVEL", raising=False)

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert os.environ["RECEPTIONIST_DATABASE_URL"] == database_url

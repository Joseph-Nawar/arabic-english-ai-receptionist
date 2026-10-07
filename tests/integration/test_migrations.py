from __future__ import annotations

import os

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from receptionist.core.config import assert_safe_test_database

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "business_config",
    "service",
    "contact",
    "conversation",
    "conversation_turn",
    "booking",
    "handoff",
    "tool_execution",
    "audit_event",
    "provider_event_receipt",
    "outbox_event",
}


def test_alembic_upgrade_downgrade_upgrade_cycle(integration_settings, monkeypatch) -> None:
    assert_safe_test_database(integration_settings)
    database_url = integration_settings.database_url_value
    monkeypatch.setenv("RECEPTIONIST_APP_ENV", integration_settings.app_env)
    monkeypatch.setenv("RECEPTIONIST_DATABASE_URL", database_url)
    monkeypatch.delenv("RECEPTIONIST_LOG_LEVEL", raising=False)

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    downgraded_engine = create_engine(database_url)
    try:
        with downgraded_engine.connect() as connection:
            assert EXPECTED_TABLES.isdisjoint(set(inspect(connection).get_table_names()))
    finally:
        downgraded_engine.dispose()

    command.upgrade(config, "head")

    assert os.environ["RECEPTIONIST_DATABASE_URL"] == database_url

    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head is not None

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert version == head
            assert set(inspect(connection).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()

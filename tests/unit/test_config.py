from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from receptionist.core.config import Settings, assert_safe_test_database
from receptionist.core.logging import configure_logging, log_configuration

TEST_DATABASE_URL = (
    "postgresql+psycopg://receptionist:unit-only-secret@localhost:55432/receptionist_test"
)
pytestmark = pytest.mark.unit


def test_valid_settings_load_without_ambient_configuration() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="DEBUG",
        database_url=TEST_DATABASE_URL,
    )

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.database_url.get_secret_value() == TEST_DATABASE_URL


def test_missing_database_configuration_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEPTIONIST_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, app_env="test", log_level="INFO")


@pytest.mark.parametrize(
    ("field", "value"),
    [("app_env", "staging"), ("log_level", "TRACE")],
)
def test_invalid_environment_or_log_level_is_rejected(field: str, value: str) -> None:
    values = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "INFO",
        "database_url": TEST_DATABASE_URL,
        field: value,
    }

    with pytest.raises(ValidationError, match=field):
        Settings(**values)


def test_settings_tests_ignore_dotenv_and_ambient_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "RECEPTIONIST_DATABASE_URL=postgresql+psycopg://root:dotenv-secret@localhost/receptionist_test\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("RECEPTIONIST_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, app_env="test", log_level="INFO")


def test_database_secret_is_absent_from_repr_and_configuration_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
    )
    configure_logging(settings.log_level)

    with caplog.at_level(logging.INFO):
        log_configuration(settings)

    assert "unit-only-secret" not in repr(settings)
    assert "unit-only-secret" not in caplog.text
    assert "database_url" not in caplog.text


def test_test_database_guard_rejects_development_database() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url="postgresql+psycopg://receptionist:test@localhost:5432/receptionist",
    )

    with pytest.raises(RuntimeError, match="receptionist_test"):
        assert_safe_test_database(settings)


def test_test_database_guard_accepts_ci_test_database() -> None:
    settings = Settings(
        _env_file=None,
        app_env="ci",
        log_level="INFO",
        database_url="postgresql+psycopg://receptionist:test@localhost:55432/receptionist_test",
    )

    assert_safe_test_database(settings)

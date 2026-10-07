from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from receptionist.core.config import Settings, assert_safe_test_database
from receptionist.core.logging import configure_logging, log_configuration

TEST_DATABASE_URL = (
    "postgresql+psycopg://receptionist:unit-only-secret@localhost:55432/receptionist_test"
)
TEST_CLIENT_SECRET = SecretStr("test-client-placeholder")
TEST_REFRESH_TOKEN = SecretStr("test-refresh-placeholder")
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


def test_google_calendar_settings_are_optional_for_provider_free_runs() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        google_calendar_id=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_oauth_refresh_token=None,
    )

    assert settings.google_calendar_id is None
    assert settings.google_oauth_client_id is None
    assert settings.google_oauth_client_secret is None
    assert settings.google_oauth_refresh_token is None
    assert settings.google_calendar_request_timeout_seconds == 10.0
    assert settings.google_calendar_configured is False
    assert settings.safe_summary()["google_calendar_configured"] is False


@pytest.mark.parametrize(
    "configured_values",
    [
        {"google_calendar_id": "calendar-id"},
        {
            "google_calendar_id": "calendar-id",
            "google_oauth_client_id": "client-id",
        },
        {
            "google_calendar_id": "calendar-id",
            "google_oauth_client_id": "client-id",
            "google_oauth_client_secret": TEST_CLIENT_SECRET,
        },
    ],
)
def test_google_calendar_settings_require_all_credentials_for_configured_predicate(
    configured_values: dict[str, object],
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        **configured_values,
    )

    assert settings.google_calendar_configured is False

    complete = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        google_calendar_id="calendar-id",
        google_oauth_client_id="client-id",
        google_oauth_client_secret=TEST_CLIENT_SECRET,
        google_oauth_refresh_token=TEST_REFRESH_TOKEN,
    )
    assert complete.google_calendar_configured is True


def test_google_calendar_secrets_are_absent_from_repr_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        google_calendar_id="calendar-id",
        google_oauth_client_id="client-id",
        google_oauth_client_secret=TEST_CLIENT_SECRET,
        google_oauth_refresh_token=TEST_REFRESH_TOKEN,
    )
    configure_logging(settings.log_level)

    with caplog.at_level(logging.INFO):
        log_configuration(settings)

    assert TEST_CLIENT_SECRET.get_secret_value() not in repr(settings)
    assert TEST_REFRESH_TOKEN.get_secret_value() not in repr(settings)
    assert TEST_CLIENT_SECRET.get_secret_value() not in caplog.text
    assert TEST_REFRESH_TOKEN.get_secret_value() not in caplog.text
    assert "google_oauth" not in caplog.text
    assert "google_calendar_id" not in caplog.text


def test_settings_tests_ignore_dotenv_and_ambient_google_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "RECEPTIONIST_GOOGLE_CALENDAR_ID=dotenv-calendar\n"
        "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_ID=dotenv-client\n"
        "RECEPTIONIST_GOOGLE_OAUTH_CLIENT_SECRET=dotenv-placeholder\n"
        "RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN=dotenv-placeholder\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECEPTIONIST_GOOGLE_CALENDAR_ID", "ambient-calendar")
    monkeypatch.setenv("RECEPTIONIST_GOOGLE_OAUTH_CLIENT_ID", "ambient-client")
    monkeypatch.setenv("RECEPTIONIST_GOOGLE_OAUTH_CLIENT_SECRET", "ambient-placeholder")
    monkeypatch.setenv("RECEPTIONIST_GOOGLE_OAUTH_REFRESH_TOKEN", "ambient-placeholder")

    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=TEST_DATABASE_URL,
        google_calendar_id=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_oauth_refresh_token=None,
    )

    assert settings.google_calendar_id is None
    assert settings.google_oauth_client_id is None
    assert settings.google_oauth_client_secret is None
    assert settings.google_oauth_refresh_token is None


@pytest.mark.parametrize(
    ("timeout", "error_type"),
    [(0, "greater_than"), (-1, "greater_than"), (30.1, "less_than_equal")],
)
def test_calendar_timeout_is_positive_and_bounded(timeout: float, error_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            app_env="test",
            log_level="INFO",
            database_url=TEST_DATABASE_URL,
            google_calendar_request_timeout_seconds=timeout,
        )
    assert any(error["type"] == error_type for error in exc_info.value.errors())


def test_google_calendar_id_cannot_be_blank() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            app_env="test",
            log_level="INFO",
            database_url=TEST_DATABASE_URL,
            google_calendar_id="   ",
        )
    assert any(
        error["loc"] == ("google_calendar_id",) and error["type"] == "value_error"
        for error in exc_info.value.errors()
    )


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

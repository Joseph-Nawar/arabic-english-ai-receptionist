"""Application settings and safety checks."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

AppEnvironment = Literal["local", "test", "ci", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime settings loaded from explicit inputs or ``RECEPTIONIST_`` variables."""

    app_env: AppEnvironment = "local"
    log_level: LogLevel = "INFO"
    database_url: SecretStr = Field(repr=False)
    google_calendar_id: str | None = None
    google_oauth_client_id: SecretStr | None = Field(default=None, repr=False)
    google_oauth_client_secret: SecretStr | None = Field(default=None, repr=False)
    google_oauth_refresh_token: SecretStr | None = Field(default=None, repr=False)
    google_calendar_request_timeout_seconds: float = Field(default=10.0, gt=0, le=30)

    model_config = SettingsConfigDict(
        env_prefix="RECEPTIONIST_",
        env_file=".env",
        env_ignore_empty=True,
        extra="forbid",
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        """Accept only a complete PostgreSQL SQLAlchemy URL."""
        parsed = make_url(value.get_secret_value())
        if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("database_url must use the postgresql+psycopg driver")
        if not parsed.database:
            raise ValueError("database_url must include a database name")
        return value

    @field_validator("google_calendar_id")
    @classmethod
    def validate_google_calendar_id(cls, value: str | None) -> str | None:
        """Reject a configured Calendar ID that contains no non-whitespace value."""
        if value is not None and not value.strip():
            raise ValueError("google_calendar_id must not be blank")
        return value

    @property
    def database_url_value(self) -> str:
        """Return the URL for SQLAlchemy internals; callers must not log it."""
        return self.database_url.get_secret_value()

    @property
    def google_calendar_configured(self) -> bool:
        """Return whether all non-blank Google Calendar credentials are present."""
        required_values = (
            self.google_calendar_id,
            self.google_oauth_client_id,
            self.google_oauth_client_secret,
            self.google_oauth_refresh_token,
        )
        return all(
            value is not None
            and (not isinstance(value, SecretStr) or bool(value.get_secret_value().strip()))
            and (not isinstance(value, str) or bool(value.strip()))
            for value in required_values
        )

    def safe_summary(self) -> dict[str, str | bool | float]:
        """Return configuration details safe for ordinary logs."""
        return {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "database_configured": True,
            "google_calendar_configured": self.google_calendar_configured,
            "google_calendar_request_timeout_seconds": self.google_calendar_request_timeout_seconds,
        }


def get_settings() -> Settings:
    """Load settings from the process environment for the default ASGI app."""
    # pydantic-settings fills this required field from the process environment or .env.
    return Settings()  # type: ignore[call-arg]


def assert_safe_test_database(settings: Settings) -> None:
    """Refuse destructive integration-test setup outside the isolated test database."""
    if settings.app_env not in {"test", "ci"}:
        raise RuntimeError("destructive database operations require test or ci environment")

    database_name = make_url(settings.database_url_value).database
    if database_name != "receptionist_test":
        raise RuntimeError("destructive database operations require receptionist_test")

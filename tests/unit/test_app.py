from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from receptionist.core.config import Settings
from receptionist.main import create_app

pytestmark = pytest.mark.unit


UNREACHABLE_DATABASE_URL = "postgresql+psycopg://receptionist:unit@127.0.0.1:1/receptionist_test"


def test_create_app_accepts_explicit_settings() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=UNREACHABLE_DATABASE_URL,
    )

    app = create_app(settings)

    assert isinstance(app, FastAPI)


async def test_liveness_returns_200_without_database_connectivity() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url=UNREACHABLE_DATABASE_URL,
    )

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

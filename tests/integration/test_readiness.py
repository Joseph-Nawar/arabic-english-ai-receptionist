from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from receptionist.core.config import Settings
from receptionist.main import create_app

pytestmark = pytest.mark.integration


async def _get(app, path: str):
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        return await client.get(path)


async def test_readiness_returns_200_with_reachable_postgresql(integration_settings) -> None:
    response = await _get(create_app(integration_settings), "/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readiness_returns_503_and_liveness_stays_200_when_database_is_unavailable() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="INFO",
        database_url="postgresql+psycopg://receptionist:test@127.0.0.1:1/receptionist_test",
    )
    app = create_app(settings)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        live_response = await client.get("/health/live")
        ready_response = await client.get("/health/ready")

    assert live_response.status_code == 200
    assert ready_response.status_code == 503
    assert ready_response.json() == {"status": "unavailable"}

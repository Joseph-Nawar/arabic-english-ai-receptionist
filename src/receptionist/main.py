"""FastAPI application factory and ASGI entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from receptionist.api.health import router as health_router
from receptionist.core.config import Settings, get_settings
from receptionist.core.logging import (
    configure_logging,
    log_configuration,
    log_shutdown,
    log_startup,
)
from receptionist.db.session import create_database_resources, dispose_database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the ASGI application with optional explicit test settings."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings or get_settings()
        configure_logging(resolved_settings.log_level)
        log_configuration(resolved_settings)
        resources = create_database_resources(resolved_settings)
        application.state.settings = resolved_settings
        application.state.engine = resources.engine
        application.state.session_factory = resources.session_factory
        log_startup()
        try:
            yield
        finally:
            await dispose_database(resources)
            log_shutdown()

    application = FastAPI(
        title="Arabic-English AI Receptionist",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    return application


app = create_app()


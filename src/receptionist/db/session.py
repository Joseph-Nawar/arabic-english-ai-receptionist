"""Async SQLAlchemy engine and session-factory lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from receptionist.core.config import Settings

READINESS_TIMEOUT_SECONDS = 2.0
LOGGER = logging.getLogger("receptionist.database")


@dataclass(slots=True)
class DatabaseResources:
    """Process-lifetime database objects owned by the FastAPI lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def create_database_resources(settings: Settings) -> DatabaseResources:
    """Create async database resources without making a network connection."""
    engine = create_async_engine(settings.database_url_value, pool_pre_ping=True)
    return DatabaseResources(
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )


async def check_database(engine: AsyncEngine) -> bool:
    """Run a bounded, lightweight PostgreSQL readiness probe."""
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError, TimeoutError) as exc:
        LOGGER.warning("Database readiness probe failed: %s", type(exc).__name__)
        return False
    return True


async def dispose_database(resources: DatabaseResources) -> None:
    """Dispose all pooled database connections during process shutdown."""
    await resources.engine.dispose()

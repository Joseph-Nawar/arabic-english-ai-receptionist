"""Process liveness and database readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from receptionist.db.session import check_database

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Report that the web process can serve requests."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    """Report whether the configured PostgreSQL database is reachable."""
    engine: AsyncEngine = request.app.state.engine
    if await check_database(engine):
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

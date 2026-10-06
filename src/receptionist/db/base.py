"""Declarative metadata root for future application tables."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Empty metadata root retained for Alembic and later schema phases."""


"""Declarative metadata root for application tables."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Metadata root used by Alembic and the domain schema."""

"""Conflict-safe Contact identity resolution."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from receptionist.db.models import Contact
from receptionist.domain.identity import normalize_phone_number


async def resolve_or_create_contact(
    session: AsyncSession, raw_number: str | None, default_region: str
) -> Contact:
    """Resolve a Contact by canonical phone identity without owning the transaction."""
    if raw_number is None or not raw_number.strip():
        contact = Contact(phone_e164=None)
        session.add(contact)
        await session.flush()
        return contact

    phone_e164 = normalize_phone_number(raw_number, default_region)
    statement = (
        insert(Contact)
        .values(phone_e164=phone_e164)
        .on_conflict_do_nothing(
            index_elements=[Contact.phone_e164],
            index_where=Contact.phone_e164.is_not(None),
        )
        .returning(Contact)
    )
    inserted = await session.scalars(statement)
    inserted_contact = inserted.first()
    if inserted_contact is not None:
        return inserted_contact

    resolved_contact = await session.scalar(
        select(Contact).where(Contact.phone_e164 == phone_e164)
    )
    if resolved_contact is None:
        raise RuntimeError("contact conflict did not resolve to a persisted contact")
    return resolved_contact

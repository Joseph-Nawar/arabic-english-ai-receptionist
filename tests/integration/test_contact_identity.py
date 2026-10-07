from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from receptionist.db.contact_identity import resolve_or_create_contact
from receptionist.db.models import Contact
from receptionist.db.session import create_database_resources, dispose_database

pytestmark = pytest.mark.integration


async def test_equivalent_phone_representations_resolve_to_one_contact(
    integration_settings,
) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.session_factory.begin() as session:
            first = await resolve_or_create_contact(session, "+966501234567", "SA")
        async with resources.session_factory.begin() as session:
            second = await resolve_or_create_contact(session, "0501234567", "SA")

        assert first.id == second.id
        assert second.phone_e164 == "+966501234567"
    finally:
        await dispose_database(resources)


async def test_different_phones_resolve_to_different_contacts(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.session_factory.begin() as session:
            first = await resolve_or_create_contact(session, "+966551234567", "SA")
        async with resources.session_factory.begin() as session:
            second = await resolve_or_create_contact(session, "+966561234567", "SA")

        assert first.id != second.id
        assert first.phone_e164 != second.phone_e164
    finally:
        await dispose_database(resources)


async def test_missing_or_withheld_callers_create_distinct_contacts(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.session_factory.begin() as session:
            first = await resolve_or_create_contact(session, None, "SA")
        async with resources.session_factory.begin() as session:
            second = await resolve_or_create_contact(session, "  ", "SA")

        assert first.id != second.id
        assert first.phone_e164 is None
        assert second.phone_e164 is None
    finally:
        await dispose_database(resources)


async def test_same_phone_resolution_is_idempotent(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        async with resources.session_factory.begin() as session:
            first = await resolve_or_create_contact(session, "+966571234567", "SA")
        async with resources.session_factory.begin() as session:
            second = await resolve_or_create_contact(session, "+966571234567", "SA")

        assert first.id == second.id
    finally:
        await dispose_database(resources)


async def test_concurrent_same_phone_workers_commit_before_return(integration_settings) -> None:
    resources = create_database_resources(integration_settings)
    canonical = f"+96658{uuid.uuid4().int % 10**7:07d}"

    async def worker() -> uuid.UUID:
        async with resources.session_factory() as session:
            async with session.begin():
                contact = await resolve_or_create_contact(session, canonical, "SA")
                contact_id = contact.id
            return contact_id

    try:
        async with resources.session_factory() as session:
            existing = await session.scalar(
                select(Contact.id).where(Contact.phone_e164 == canonical)
            )
        assert existing is None

        resolved_ids = await asyncio.wait_for(asyncio.gather(worker(), worker()), timeout=5)

        assert resolved_ids[0] == resolved_ids[1]
        async with resources.session_factory() as session:
            contacts = (
                await session.scalars(select(Contact).where(Contact.phone_e164 == canonical))
            ).all()

        assert len(contacts) == 1
        assert contacts[0].id == resolved_ids[0]
    finally:
        await dispose_database(resources)


async def test_invalid_phone_fails_before_persistence(integration_settings) -> None:
    resources = create_database_resources(integration_settings)

    try:
        with pytest.raises(ValueError, match="could not be parsed"):
            async with resources.session_factory.begin() as session:
                await resolve_or_create_contact(session, "not-a-phone", "SA")
    finally:
        await dispose_database(resources)

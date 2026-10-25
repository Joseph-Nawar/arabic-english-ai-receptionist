from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from receptionist.db.models import (
    Contact,
    CRMContactLink,
    CRMContactLinkState,
)
from receptionist.domain.identity import CRMContactLinkProvenance

pytestmark = pytest.mark.integration


def link_values(
    contact_id: uuid.UUID,
    *,
    portal_id: str = "123456",
    provider_id: str | None = None,
    linked_by: CRMContactLinkProvenance = CRMContactLinkProvenance.PHONE_E164,
    state: CRMContactLinkState = CRMContactLinkState.ACTIVE,
) -> dict[str, object]:
    return {
        "contact_id": contact_id,
        "hubspot_portal_id": portal_id,
        "hubspot_contact_id": (
            provider_id if provider_id is not None else str(uuid.uuid4().int % 10**20)
        ),
        "linked_by": linked_by,
        "state": state,
        "verified_at": datetime.now(UTC),
    }


async def test_valid_contact_link_persists_scoped_identity_and_timestamps(session_factory) -> None:
    contact_id = uuid.uuid4()
    async with session_factory.begin() as session:
        session.add(Contact(id=contact_id))
        await session.flush()
        link = CRMContactLink(**link_values(contact_id))
        session.add(link)
        await session.flush()

        assert link.contact_id == contact_id
        assert link.hubspot_portal_id == "123456"
        assert link.hubspot_contact_id
        assert link.linked_by is CRMContactLinkProvenance.PHONE_E164
        assert link.state is CRMContactLinkState.ACTIVE
        assert link.verified_at is not None and link.verified_at.tzinfo is not None
        assert link.created_at is not None and link.created_at.tzinfo is not None
        assert link.updated_at is not None and link.updated_at.tzinfo is not None


async def test_contact_link_rejects_duplicate_local_contact(session_factory) -> None:
    contact_id = uuid.uuid4()
    async with session_factory.begin() as session:
        session.add(Contact(id=contact_id))
        await session.flush()
        session.add(CRMContactLink(**link_values(contact_id)))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO crm_contact_link "
                    "(contact_id, hubspot_portal_id, hubspot_contact_id, linked_by, state) "
                    "VALUES (:contact_id, :portal, :provider, :linked_by, :state)"
                ),
                {
                    "contact_id": contact_id,
                    "portal": "123456",
                    "provider": "different-provider-id",
                    "linked_by": "phone_e164",
                    "state": "active",
                },
            )


async def test_contact_link_rejects_duplicate_provider_contact_within_portal(
    session_factory,
) -> None:
    first_contact_id, second_contact_id = uuid.uuid4(), uuid.uuid4()
    provider_id = str(uuid.uuid4().int % 10**20)
    async with session_factory.begin() as session:
        session.add_all([Contact(id=first_contact_id), Contact(id=second_contact_id)])
        await session.flush()
        session.add(CRMContactLink(**link_values(first_contact_id, provider_id=provider_id)))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            session.add(CRMContactLink(**link_values(second_contact_id, provider_id=provider_id)))
            await session.flush()


async def test_same_provider_contact_id_is_allowed_in_distinct_portals(session_factory) -> None:
    first_contact_id, second_contact_id = uuid.uuid4(), uuid.uuid4()
    provider_id = str(uuid.uuid4().int % 10**20)
    async with session_factory.begin() as session:
        session.add_all([Contact(id=first_contact_id), Contact(id=second_contact_id)])
        await session.flush()
        session.add_all(
            [
                CRMContactLink(
                    **link_values(
                        first_contact_id,
                        portal_id="123456",
                        provider_id=provider_id,
                    )
                ),
                CRMContactLink(
                    **link_values(
                        second_contact_id,
                        portal_id="654321",
                        provider_id=provider_id,
                    )
                ),
            ]
        )
        await session.flush()


async def test_contact_link_enforces_contact_foreign_key(session_factory) -> None:
    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            session.add(CRMContactLink(**link_values(uuid.uuid4())))
            await session.flush()


async def test_contact_link_foreign_key_restricts_contact_deletion(session_factory) -> None:
    contact_id = uuid.uuid4()
    async with session_factory.begin() as session:
        session.add(Contact(id=contact_id))
        await session.flush()
        session.add(CRMContactLink(**link_values(contact_id)))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(delete(Contact).where(Contact.id == contact_id))


@pytest.mark.parametrize(
    ("linked_by", "state"),
    [("unknown", "active"), ("phone_e164", "bogus")],
)
async def test_contact_link_database_rejects_invalid_controlled_values(
    session_factory,
    linked_by: str,
    state: str,
) -> None:
    contact_id = uuid.uuid4()
    provider_id = str(uuid.uuid4().int % 10**20)
    async with session_factory.begin() as session:
        session.add(Contact(id=contact_id))
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO crm_contact_link "
                    "(contact_id, hubspot_portal_id, hubspot_contact_id, linked_by, state) "
                    "VALUES (:contact_id, :portal, :provider, :linked_by, :state)"
                ),
                {
                    "contact_id": contact_id,
                    "portal": "123456",
                    "provider": provider_id,
                    "linked_by": linked_by,
                    "state": state,
                },
            )


@pytest.mark.parametrize("provider_id", ["", "  ", "\t\n"])
def test_contact_link_rejects_blank_provider_record_id(provider_id: str) -> None:
    with pytest.raises(ValueError, match="provider Contact ID"):
        CRMContactLink(**link_values(uuid.uuid4(), provider_id=provider_id))


@pytest.mark.parametrize("portal_id", ["", "0", "12x", " 123456"])
def test_contact_link_rejects_nonpositive_or_nondigit_portal_id(portal_id: str) -> None:
    with pytest.raises(ValueError, match="portal ID"):
        CRMContactLink(**link_values(uuid.uuid4(), portal_id=portal_id))

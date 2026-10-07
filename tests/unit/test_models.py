from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Enum, SmallInteger
from sqlalchemy.dialects.postgresql import JSONB, UUID

from receptionist.db import models
from receptionist.db.base import Base

pytestmark = pytest.mark.unit


EXPECTED_TABLES = {
    "business_config",
    "service",
    "contact",
    "conversation",
    "conversation_turn",
    "booking",
    "handoff",
    "tool_execution",
    "audit_event",
    "provider_event_receipt",
    "outbox_event",
}


def _index_names(table_name: str) -> set[str]:
    return {index.name for index in Base.metadata.tables[table_name].indexes}


def _constraint_text(table_name: str) -> list[str]:
    table = Base.metadata.tables[table_name]
    return [
        str(sqltext)
        for constraint in table.constraints
        if (sqltext := getattr(constraint, "sqltext", None)) is not None
    ]


def test_phase_one_tables_are_registered() -> None:
    assert set(Base.metadata.tables) >= EXPECTED_TABLES
    assert models.BusinessConfig.__tablename__ == "business_config"


@pytest.mark.parametrize(
    "table_name",
    sorted(EXPECTED_TABLES - {"business_config"}),
)
def test_normal_domain_entities_use_postgresql_uuid_primary_keys(table_name: str) -> None:
    table = Base.metadata.tables[table_name]

    assert isinstance(table.c.id.type, UUID)
    assert table.c.id.default is not None


def test_business_config_uses_singleton_small_integer_and_jsonb_documents() -> None:
    table = Base.metadata.tables["business_config"]
    constraints = _constraint_text("business_config")

    assert isinstance(table.c.id.type, SmallInteger)
    assert any("id = 1" in text for text in constraints)
    assert isinstance(table.c.weekly_hours.type, JSONB)
    assert isinstance(table.c.service_areas.type, JSONB)
    assert "service_id" not in table.c
    assert "tenant_id" not in table.c


def test_event_and_lifecycle_timestamps_are_timezone_aware() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True, (
                    f"{table.name}.{column.name} is not timezone-aware"
                )


def test_all_persisted_enums_are_constrained_strings_not_native_enums() -> None:
    enum_columns = [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Enum)
    ]

    assert enum_columns
    assert all(column.type.native_enum is False for column in enum_columns)
    assert all(column.type.create_constraint is True for column in enum_columns)


def test_required_indexes_are_registered() -> None:
    expected = {
        "uq_contact_phone_e164_non_null": "contact",
        "ix_conversation_contact_opened": "conversation",
        "ix_conversation_status_control_mode": "conversation",
        "uq_conversation_turn_sequence": "conversation_turn",
        "ix_booking_contact_status": "booking",
        "uq_handoff_active_conversation": "handoff",
        "ix_handoff_status_priority_requested": "handoff",
        "uq_tool_execution_idempotency_key_non_null": "tool_execution",
        "ix_tool_execution_conversation": "tool_execution",
        "ix_audit_event_conversation_occurred": "audit_event",
        "uq_provider_event_receipt_provider_external": "provider_event_receipt",
        "ix_outbox_event_unpublished": "outbox_event",
    }

    for index_name, table_name in expected.items():
        assert index_name in _index_names(table_name)


def test_partial_index_predicates_are_explicit() -> None:
    expected_predicates = {
        ("contact", "uq_contact_phone_e164_non_null"): "phone_e164 IS NOT NULL",
        ("handoff", "uq_handoff_active_conversation"): "status IN ('pending', 'accepted')",
        (
            "tool_execution",
            "uq_tool_execution_idempotency_key_non_null",
        ): "idempotency_key IS NOT NULL",
        ("outbox_event", "ix_outbox_event_unpublished"): "published_at IS NULL",
    }

    for (table_name, index_name), expected in expected_predicates.items():
        index = next(
            index for index in Base.metadata.tables[table_name].indexes if index.name == index_name
        )
        assert str(index.dialect_options["postgresql"]["where"]) == expected


def test_pending_action_and_booking_invariants_are_metadata_constraints() -> None:
    conversation_checks = " ".join(_constraint_text("conversation"))
    booking_checks = " ".join(_constraint_text("booking"))

    assert "pending_action_type" in conversation_checks
    assert "pending_action_confirmed_at" in conversation_checks
    assert "closed_at" in conversation_checks
    assert "start_at" in booking_checks
    assert "end_at" in booking_checks
    assert "start_at < end_at" in booking_checks


def test_historical_foreign_keys_are_restrictive() -> None:
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.ondelete in {None, "RESTRICT"}


def test_booking_and_future_seams_have_no_provider_payload_or_identifier_columns() -> None:
    booking_columns = set(Base.metadata.tables["booking"].columns.keys())
    assert not booking_columns & {"google_event_id", "calendar_id", "calendar_provider"}

    for table_name in {"provider_event_receipt", "outbox_event", "tool_execution"}:
        columns = set(Base.metadata.tables[table_name].columns.keys())
        assert not columns & {"raw_body", "raw_payload", "credentials", "authorization"}

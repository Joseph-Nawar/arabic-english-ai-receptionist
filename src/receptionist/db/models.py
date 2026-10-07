"""Concrete Phase 1 SQLAlchemy records and PostgreSQL constraints."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from receptionist.db.base import Base
from receptionist.domain.enums import (
    AuditActorType,
    BookingStatus,
    BusinessLanguage,
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationOutcome,
    ConversationStatus,
    ConversationTurnRole,
    HandoffPriority,
    HandoffReason,
    HandoffStatus,
    PendingActionStatus,
    PendingActionType,
    ToolExecutionStatus,
)


def _enum_type(enum_class: type[StrEnum], name: str) -> SAEnum:
    """Persist a Python string enum as a constrained VARCHAR, never a native enum."""
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def _uuid_column() -> MappedColumn[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at_column() -> MappedColumn[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at_column() -> MappedColumn[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BusinessConfig(Base):
    __tablename__ = "business_config"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    default_phone_region: Mapped[str] = mapped_column(String(2), nullable=False)
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    supported_languages: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    default_language: Mapped[BusinessLanguage] = mapped_column(
        _enum_type(BusinessLanguage, "business_language"), nullable=False
    )
    weekly_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_hours_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    service_areas: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    booking_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    handoff_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    greeting_templates: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    closing_templates: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    retention_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = _updated_at_column()

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_business_config_singleton"),
    )


class Service(Base):
    __tablename__ = "service"

    id: Mapped[uuid.UUID] = _uuid_column()
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    description_en: Mapped[str] = mapped_column(Text, nullable=False)
    description_ar: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    bookable: Mapped[bool] = mapped_column(nullable=False)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    pricing: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    booking_requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = _updated_at_column()

    __table_args__ = (
        UniqueConstraint("code", name="uq_service_code"),
        CheckConstraint("default_duration_minutes > 0", name="ck_service_duration_positive"),
    )


class Contact(Base):
    __tablename__ = "contact"

    id: Mapped[uuid.UUID] = _uuid_column()
    phone_e164: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    preferred_language: Mapped[BusinessLanguage | None] = mapped_column(
        _enum_type(BusinessLanguage, "contact_preferred_language"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = _updated_at_column()

    __table_args__ = (
        Index(
            "uq_contact_phone_e164_non_null",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL"),
        ),
    )


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[uuid.UUID] = _uuid_column()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contact.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[ConversationChannel] = mapped_column(
        _enum_type(ConversationChannel, "conversation_channel"), nullable=False
    )
    status: Mapped[ConversationStatus] = mapped_column(
        _enum_type(ConversationStatus, "conversation_status"), nullable=False
    )
    control_mode: Mapped[ControlMode] = mapped_column(
        _enum_type(ControlMode, "conversation_control_mode"), nullable=False
    )
    language_mode: Mapped[ConversationLanguageMode] = mapped_column(
        _enum_type(ConversationLanguageMode, "conversation_language_mode"), nullable=False
    )
    outcome: Mapped[ConversationOutcome | None] = mapped_column(
        _enum_type(ConversationOutcome, "conversation_outcome"), nullable=True
    )
    recent_summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    pending_action_type: Mapped[PendingActionType | None] = mapped_column(
        _enum_type(PendingActionType, "pending_action_type"), nullable=True
    )
    pending_action_status: Mapped[PendingActionStatus | None] = mapped_column(
        _enum_type(PendingActionStatus, "pending_action_status"), nullable=True
    )
    pending_action_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    pending_action_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_action_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_action_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opened_at: Mapped[datetime] = _created_at_column()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = _updated_at_column()

    __table_args__ = (
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR "
            "(status = 'closed' AND closed_at IS NOT NULL)",
            name="ck_conversation_close_time",
        ),
        CheckConstraint(
            "(pending_action_type IS NULL AND pending_action_status IS NULL "
            "AND pending_action_payload IS NULL AND pending_action_created_at IS NULL "
            "AND pending_action_confirmed_at IS NULL AND pending_action_expires_at IS NULL) "
            "OR (pending_action_type IS NOT NULL AND pending_action_status IS NOT NULL "
            "AND pending_action_payload IS NOT NULL AND pending_action_created_at IS NOT NULL "
            "AND ((pending_action_status = 'confirmed' "
            "AND pending_action_confirmed_at IS NOT NULL) "
            "OR (pending_action_status = 'awaiting_confirmation' "
            "AND pending_action_confirmed_at IS NULL)))",
            name="ck_conversation_pending_action",
        ),
        Index("ix_conversation_contact_opened", "contact_id", "opened_at"),
        Index("ix_conversation_status_control_mode", "status", "control_mode"),
    )


class ConversationTurn(Base):
    __tablename__ = "conversation_turn"

    id: Mapped[uuid.UUID] = _uuid_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[ConversationTurnRole] = mapped_column(
        _enum_type(ConversationTurnRole, "conversation_turn_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language_mode: Mapped[ConversationLanguageMode | None] = mapped_column(
        _enum_type(ConversationLanguageMode, "turn_language_mode"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at_column()

    __table_args__ = (
        Index(
            "uq_conversation_turn_sequence",
            "conversation_id",
            "sequence_number",
            unique=True,
        ),
        CheckConstraint("sequence_number > 0", name="ck_conversation_turn_sequence_positive"),
    )


class Booking(Base):
    __tablename__ = "booking"

    id: Mapped[uuid.UUID] = _uuid_column()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contact.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[BookingStatus] = mapped_column(
        _enum_type(BookingStatus, "booking_status"), nullable=False
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    booking_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()
    updated_at: Mapped[datetime] = _updated_at_column()

    __table_args__ = (
        CheckConstraint(
            "(start_at IS NULL AND end_at IS NULL) OR "
            "(start_at IS NOT NULL AND end_at IS NOT NULL)",
            name="ck_booking_time_pair",
        ),
        CheckConstraint(
            "(start_at IS NULL AND end_at IS NULL) OR start_at < end_at",
            name="ck_booking_time_order",
        ),
        Index("ix_booking_contact_status", "contact_id", "status"),
    )


class Handoff(Base):
    __tablename__ = "handoff"

    id: Mapped[uuid.UUID] = _uuid_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[HandoffStatus] = mapped_column(
        _enum_type(HandoffStatus, "handoff_status"), nullable=False
    )
    reason: Mapped[HandoffReason] = mapped_column(
        _enum_type(HandoffReason, "handoff_reason"), nullable=False
    )
    priority: Mapped[HandoffPriority] = mapped_column(
        _enum_type(HandoffPriority, "handoff_priority"), nullable=False
    )
    safe_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    safe_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    requested_at: Mapped[datetime] = _created_at_column()
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_handoff_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'accepted')"),
        ),
        Index("ix_handoff_status_priority_requested", "status", "priority", "requested_at"),
    )


class ToolExecution(Base):
    __tablename__ = "tool_execution"

    id: Mapped[uuid.UUID] = _uuid_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ToolExecutionStatus] = mapped_column(
        _enum_type(ToolExecutionStatus, "tool_execution_status"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sanitized_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sanitized_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime] = _created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_tool_execution_idempotency_key_non_null",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_tool_execution_conversation", "conversation_id"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = _uuid_column()
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_type: Mapped[AuditActorType] = mapped_column(
        _enum_type(AuditActorType, "audit_actor_type"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contact.id", ondelete="RESTRICT"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entity_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sanitized_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = _created_at_column()

    __table_args__ = (
        Index("ix_audit_event_conversation_occurred", "conversation_id", "occurred_at"),
    )


class ProviderEventReceipt(Base):
    __tablename__ = "provider_event_receipt"

    id: Mapped[uuid.UUID] = _uuid_column()
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = _created_at_column()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_provider_event_receipt_provider_external",
            "provider",
            "external_event_id",
            unique=True,
        ),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_event"

    id: Mapped[uuid.UUID] = _uuid_column()
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at_column()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        Index(
            "ix_outbox_event_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

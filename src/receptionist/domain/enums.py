"""Controlled vocabularies for Phase 1 domain and persistence state."""

from enum import StrEnum


class AfterHoursMode(StrEnum):
    HANDOFF = "handoff"
    CALLBACK = "callback"
    CLOSED_MESSAGE = "closed_message"


class BusinessLanguage(StrEnum):
    ENGLISH = "en"
    ARABIC = "ar"


class Weekday(StrEnum):
    SUNDAY = "sunday"
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"


class PricingMode(StrEnum):
    FIXED = "fixed"
    FROM = "from"
    RANGE = "range"
    QUOTE_REQUIRED = "quote_required"
    NOT_PUBLISHED = "not_published"


class ConversationChannel(StrEnum):
    PHONE = "phone"
    WHATSAPP = "whatsapp"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ControlMode(StrEnum):
    AI = "ai"
    HANDOFF_PENDING = "handoff_pending"
    HUMAN = "human"


class ConversationLanguageMode(StrEnum):
    UNKNOWN = "unknown"
    ENGLISH = "en"
    ARABIC = "ar"
    MIXED = "mixed"


class ConversationOutcome(StrEnum):
    INFORMATION_ONLY = "information_only"
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    HANDOFF = "handoff"
    UNRESOLVED = "unresolved"
    ABANDONED = "abandoned"


class PendingActionType(StrEnum):
    CREATE_BOOKING = "create_booking"
    RESCHEDULE_BOOKING = "reschedule_booking"
    CANCEL_BOOKING = "cancel_booking"


class PendingActionStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"


class ConversationTurnRole(StrEnum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    HUMAN = "human"
    SYSTEM = "system"


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class HandoffStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class HandoffReason(StrEnum):
    EXPLICIT_REQUEST = "explicit_request"
    REPEATED_MISUNDERSTANDING = "repeated_misunderstanding"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN_INFORMATION = "unknown_information"
    COMPLAINT = "complaint"
    UNUSUAL_OR_HIGH_RISK = "unusual_or_high_risk"
    URGENT_OR_EMERGENCY = "urgent_or_emergency"
    INTEGRATION_FAILURE = "integration_failure"
    CANNOT_SAFELY_ACT = "cannot_safely_act"


class HandoffPriority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ToolExecutionStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class AuditActorType(StrEnum):
    SYSTEM = "system"
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    HUMAN = "human"
    PROVIDER = "provider"

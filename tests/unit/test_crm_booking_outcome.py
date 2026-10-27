from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from receptionist.application.crm_booking_evidence import (
    BookingAction,
    BookingEvidence,
    HistoricalBookingStatus,
)
from receptionist.application.crm_booking_outcome import (
    BookingOutcome,
    BookingOutcomeErrorCode,
    BookingOutcomeRequest,
    BookingOutcomeResult,
    _format_booking_note,
    _note_idempotency_key,
    _source_operation_marker,
)

pytestmark = pytest.mark.unit


BOOKING_ID = uuid.UUID("c0a80101-1111-4111-8111-000000000001")
SOURCE_ID = uuid.UUID("c0a80101-1111-4111-8111-000000000002")
CONVERSATION_ID = uuid.UUID("c0a80101-1111-4111-8111-000000000003")
CONTACT_ID = uuid.UUID("c0a80101-1111-4111-8111-000000000004")
START = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
END = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def _evidence(
    action: BookingAction = BookingAction.CREATE_BOOKING,
    *,
    status: HistoricalBookingStatus = HistoricalBookingStatus.CONFIRMED,
    start: datetime | None = START,
    end: datetime | None = END,
) -> BookingEvidence:
    return BookingEvidence(
        booking_id=BOOKING_ID,
        source_tool_execution_id=SOURCE_ID,
        conversation_id=CONVERSATION_ID,
        contact_id=CONTACT_ID,
        action=action,
        historical_status=status,
        requested_start_at_utc=start,
        requested_end_at_utc=end,
        source_finished_at_utc=datetime(2026, 10, 25, 16, 0, 0, 123456, tzinfo=UTC),
    )


def test_request_accepts_only_booking_and_source_execution_ids() -> None:
    request = BookingOutcomeRequest(booking_id=BOOKING_ID, source_tool_execution_id=SOURCE_ID)

    assert request.booking_id == BOOKING_ID
    assert request.source_tool_execution_id == SOURCE_ID
    for extra in (
        {"contact_id": CONTACT_ID},
        {"hubspot_contact_id": "123"},
        {"action": "cancel_booking"},
        {"historical_status": "cancelled"},
        {"timestamp": "2026-10-25T16:00:00Z"},
        {"note_body": "caller text"},
        {"idempotency_key": "caller-key"},
        {"properties": {"hs_note_body": "caller text"}},
    ):
        with pytest.raises(ValidationError):
            BookingOutcomeRequest(
                booking_id=BOOKING_ID,
                source_tool_execution_id=SOURCE_ID,
                **extra,
            )


@pytest.mark.parametrize(
    ("action", "status", "action_line", "outcome_line"),
    [
        (
            BookingAction.CREATE_BOOKING,
            HistoricalBookingStatus.CONFIRMED,
            "create_booking",
            "confirmed",
        ),
        (
            BookingAction.RESCHEDULE_BOOKING,
            HistoricalBookingStatus.CONFIRMED,
            "reschedule_booking",
            "confirmed",
        ),
        (
            BookingAction.CANCEL_BOOKING,
            HistoricalBookingStatus.CANCELLED,
            "cancel_booking",
            "cancelled",
        ),
    ],
)
def test_note_body_uses_fixed_historical_fields_only(
    action: BookingAction,
    status: HistoricalBookingStatus,
    action_line: str,
    outcome_line: str,
) -> None:
    evidence = _evidence(action, status=status)

    assert _format_booking_note(evidence) == "\n".join(
        (
            f"Booking operation: {action_line}",
            f"Outcome: {outcome_line}",
            f"Booking reference: {BOOKING_ID}",
            "Requested start (UTC): 2026-10-26T07:00:00.000000Z",
            "Requested end (UTC): 2026-10-26T08:00:00.000000Z",
            f"Source operation: booking-execution:{SOURCE_ID}",
        )
    )


def test_note_body_omits_absent_historical_slot_values() -> None:
    evidence = _evidence(start=None, end=None)

    body = _format_booking_note(evidence)

    assert "Requested start (UTC):" not in body
    assert "Requested end (UTC):" not in body
    assert f"Source operation: booking-execution:{SOURCE_ID}" in body


def test_source_marker_and_operation_key_are_stable_and_namespaced() -> None:
    other_source = uuid.UUID("c0a80101-1111-4111-8111-000000000005")

    assert _source_operation_marker(SOURCE_ID) == f"booking-execution:{SOURCE_ID}"
    assert _note_idempotency_key(SOURCE_ID) == _note_idempotency_key(SOURCE_ID)
    assert _note_idempotency_key(SOURCE_ID).startswith("crm:record_booking_outcome:v1:")
    assert _note_idempotency_key(SOURCE_ID) != _note_idempotency_key(other_source)
    assert _source_operation_marker(SOURCE_ID) != _source_operation_marker(other_source)


def test_result_is_finite_and_contains_no_provider_or_customer_content() -> None:
    result = BookingOutcomeResult(
        outcome=BookingOutcome.WRITTEN,
        hubspot_note_id="654321",
    )
    serialized = result.model_dump(mode="json")

    assert serialized == {
        "operation": "record_booking_outcome",
        "outcome": "written",
        "hubspot_note_id": "654321",
        "replayed": False,
        "error_code": None,
        "next_action": "none",
    }
    assert BookingOutcomeErrorCode.LOCAL_PERSISTENCE_FAILURE.value == ("local_persistence_failure")

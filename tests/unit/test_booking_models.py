from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from receptionist.application.booking import (
    AvailabilityRequest,
    BookingError,
    BookingErrorCode,
    BookingReadResult,
    CancelBookingRequest,
    ConfirmationResult,
    ConfirmBookingActionRequest,
    CreateBookingRequest,
    GetBookingRequest,
    PreparationData,
    PreparationResult,
    RescheduleBookingRequest,
    SanitizedMetadata,
    ServiceAreaLookupRequest,
    ServiceLookupRequest,
    generate_action_token,
    pending_action_matches,
)
from receptionist.domain.enums import BookingStatus, PendingActionStatus, PendingActionType
from receptionist.domain.state import PendingActionState

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 7, 10, tzinfo=UTC)
BOOKING_ID = uuid4()
CONVERSATION_ID = uuid4()
ACTION_MARKER = "test-action-marker"


def _request_payloads() -> list[tuple[type, dict[str, object]]]:
    return [
        (ServiceLookupRequest, {"selector": "plumbing"}),
        (ServiceAreaLookupRequest, {"selector": "riyadh"}),
        (
            AvailabilityRequest,
            {"service_selector": "plumbing", "requested_start_at": NOW},
        ),
        (GetBookingRequest, {"conversation_id": CONVERSATION_ID, "booking_id": BOOKING_ID}),
        (
            CreateBookingRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "service_selector": "plumbing",
                "service_area_selector": "riyadh",
                "requested_start_at": NOW,
                "requirements": {"property_type": "villa"},
                "idempotency_key": "create-key",
            },
        ),
        (
            RescheduleBookingRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "booking_id": BOOKING_ID,
                "requested_start_at": NOW,
                "idempotency_key": "reschedule-key",
            },
        ),
        (
            CancelBookingRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "booking_id": BOOKING_ID,
                "idempotency_key": "cancel-key",
            },
        ),
        (
            ConfirmBookingActionRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "action_type": PendingActionType.CREATE_BOOKING,
                "action_token": "opaque-action-token",
                "idempotency_key": "confirm-key",
            },
        ),
    ]


def test_every_booking_request_rejects_unknown_fields() -> None:
    for request_model, payload in _request_payloads():
        with pytest.raises(ValidationError, match="extra_forbidden"):
            request_model.model_validate({**payload, "unexpected": "field"})


@pytest.mark.parametrize(
    "request_model,payload",
    [
        (
            AvailabilityRequest,
            {"service_selector": "plumbing", "requested_start_at": NOW.replace(tzinfo=None)},
        ),
        (
            CreateBookingRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "service_selector": "plumbing",
                "service_area_selector": "riyadh",
                "requested_start_at": NOW.replace(tzinfo=None),
                "idempotency_key": "create-key",
            },
        ),
        (
            RescheduleBookingRequest,
            {
                "conversation_id": CONVERSATION_ID,
                "booking_id": BOOKING_ID,
                "requested_start_at": NOW.replace(tzinfo=None),
                "idempotency_key": "reschedule-key",
            },
        ),
    ],
)
def test_datetime_requests_reject_naive_datetimes(request_model, payload) -> None:
    with pytest.raises(ValidationError, match="timezone_aware"):
        request_model.model_validate(payload)


def test_exact_confirmation_requires_action_token_and_own_idempotency_key() -> None:
    base = {
        "conversation_id": CONVERSATION_ID,
        "action_type": PendingActionType.CREATE_BOOKING,
    }

    with pytest.raises(ValidationError):
        ConfirmBookingActionRequest(**base)

    confirmation = ConfirmBookingActionRequest(
        **base,
        action_token=ACTION_MARKER,
        idempotency_key="confirmation-key",
    )
    assert confirmation.action_token == ACTION_MARKER
    assert confirmation.idempotency_key == "confirmation-key"


def test_action_tokens_are_fresh_uuid_based_and_pending_identity_is_exact() -> None:
    first = generate_action_token()
    second = generate_action_token()

    assert UUID(first)
    assert UUID(second)
    assert first != second
    assert "@" not in first
    assert "phone" not in first.casefold()
    assert "email" not in first.casefold()

    current = PendingActionState(
        type=PendingActionType.RESCHEDULE_BOOKING,
        status=PendingActionStatus.AWAITING_CONFIRMATION,
        payload={"action_token": first},
        created_at=NOW,
        confirmed_at=None,
    )
    assert pending_action_matches(current, PendingActionType.RESCHEDULE_BOOKING, first)
    assert not pending_action_matches(current, PendingActionType.RESCHEDULE_BOOKING, second)
    assert not pending_action_matches(current, PendingActionType.CANCEL_BOOKING, first)
    assert not pending_action_matches(None, PendingActionType.RESCHEDULE_BOOKING, first)


@pytest.mark.parametrize(
    "payload",
    [
        {"service_selector": "   ", "requested_start_at": NOW},
        {"service_selector": "plumbing", "requested_start_at": NOW, "service_selector_2": "x"},
    ],
)
def test_selectors_and_requests_remain_bounded(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AvailabilityRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        CreateBookingRequest(
            conversation_id=CONVERSATION_ID,
            service_selector="plumbing",
            service_area_selector="riyadh",
            requested_start_at=NOW,
            requirements={"property_type": {"raw": "nested"}},
            idempotency_key="create-key",
        )

    with pytest.raises(ValidationError):
        CreateBookingRequest(
            conversation_id=CONVERSATION_ID,
            service_selector="plumbing",
            service_area_selector="riyadh",
            requested_start_at=NOW,
            requirements={"property_type": "x" * 501},
            idempotency_key="create-key",
        )

    with pytest.raises(ValidationError):
        CancelBookingRequest(
            conversation_id=CONVERSATION_ID,
            booking_id=BOOKING_ID,
            cancellation_context="x" * 501,
            idempotency_key="cancel-key",
        )


def test_request_serialization_contains_only_safe_bounded_fields() -> None:
    request = CreateBookingRequest(
        conversation_id=CONVERSATION_ID,
        service_selector="plumbing",
        service_area_selector="riyadh",
        requested_start_at=NOW,
        requested_end_at=NOW + timedelta(hours=1),
        requirements={"property_type": "villa"},
        idempotency_key="create-key",
    )

    serialized = request.model_dump(mode="json")
    assert serialized["requested_start_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert "raw_payload" not in serialized
    assert "credentials" not in serialized
    assert "transcript" not in serialized


def test_all_stable_error_codes_serialize_as_safe_bounded_values() -> None:
    expected_codes = {
        "invalid_input",
        "unknown_service",
        "service_inactive",
        "service_not_bookable",
        "unsupported_service_area",
        "configuration_conflict",
        "invalid_requested_time",
        "outside_business_policy",
        "confirmation_required",
        "pending_action_conflict",
        "stale_pending_action",
        "booking_not_found",
        "booking_state_conflict",
        "external_reference_missing",
        "operation_in_progress",
        "idempotency_conflict",
        "duplicate_replay",
        "calendar_interval_unavailable",
        "calendar_unavailable",
        "external_event_missing",
        "external_state_conflict",
        "calendar_reconciliation_required",
    }
    assert {code.value for code in BookingErrorCode} == expected_codes

    for code in BookingErrorCode:
        error = BookingError(
            code=code,
            message="safe message",
            retryable=False,
            details={"field": "value"},
        )
        serialized = error.model_dump(mode="json")
        assert serialized == {
            "code": code.value,
            "message": "safe message",
            "retryable": False,
            "details": {"field": "value"},
        }

    with pytest.raises(ValidationError):
        BookingError(
            code=BookingErrorCode.INVALID_INPUT,
            message="safe message",
            retryable=False,
            details={"raw_payload": {"provider": "hidden"}},
        )


def test_explicit_results_serialize_without_raw_provider_objects() -> None:
    preparation = PreparationResult(
        operation="prepare_create_booking",
        replayed=False,
        data=PreparationData(
            action_type=PendingActionType.CREATE_BOOKING,
            action_token=ACTION_MARKER,
            confirmation_required=True,
            requested_start_at_utc=NOW,
            requested_end_at_utc=NOW + timedelta(hours=1),
        ),
    )
    serialized = preparation.model_dump(mode="json")
    assert serialized["data"]["confirmation_required"] is True
    assert "raw_google_event" not in serialized

    confirmation = ConfirmationResult(
        operation="confirm_booking_action",
        replayed=False,
        data={
            "booking_id": BOOKING_ID,
            "status": BookingStatus.CONFIRMED,
            "calendar_id": "calendar-id",
            "calendar_event_id": "event-id",
        },
    )
    assert confirmation.data.booking_id == BOOKING_ID

    booking_read = BookingReadResult(
        operation="get_booking",
        replayed=False,
        data={
            "booking_id": UUID(str(BOOKING_ID)),
            "status": BookingStatus.PENDING,
            "reconciliation_status": "operation_in_progress",
        },
    )
    assert booking_read.data.status is BookingStatus.PENDING


def test_sanitized_metadata_rejects_customer_and_provider_payload_fields() -> None:
    sentinel_values = {
        "phone": "+15555550199",
        "email": "customer-sentinel@example.test",
        "address": "17 Sentinel Road",
        "transcript": "customer transcript sentinel",
        "raw": "provider body sentinel",
        "etag": "provider-etag-sentinel",
    }

    for key, value in sentinel_values.items():
        with pytest.raises(ValidationError):
            SanitizedMetadata(values={key: value})

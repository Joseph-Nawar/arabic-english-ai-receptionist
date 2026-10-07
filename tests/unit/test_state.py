from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from receptionist.domain.enums import ControlMode, PendingActionStatus, PendingActionType
from receptionist.domain.state import (
    InvalidControlTransition,
    PendingActionError,
    clear_pending_action,
    confirm_pending_action,
    stage_pending_action,
    transition_control_mode,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ControlMode.AI, ControlMode.HANDOFF_PENDING),
        (ControlMode.HANDOFF_PENDING, ControlMode.HUMAN),
        (ControlMode.HANDOFF_PENDING, ControlMode.AI),
        (ControlMode.HUMAN, ControlMode.AI),
    ],
)
def test_allowed_control_transitions(current, target) -> None:
    assert transition_control_mode(current, target) is target


def test_invalid_control_transition_is_rejected() -> None:
    with pytest.raises(InvalidControlTransition):
        transition_control_mode(ControlMode.AI, ControlMode.HUMAN)


def test_stage_pending_action_requires_empty_slot() -> None:
    staged = stage_pending_action(
        None,
        PendingActionType.CREATE_BOOKING,
        {"service_code": "plumbing"},
        NOW,
    )

    assert staged.type is PendingActionType.CREATE_BOOKING
    assert staged.status is PendingActionStatus.AWAITING_CONFIRMATION
    assert staged.payload == {"service_code": "plumbing"}
    assert staged.created_at == NOW
    assert staged.confirmed_at is None
    assert staged.expires_at is None

    with pytest.raises(PendingActionError):
        stage_pending_action(
            staged,
            PendingActionType.CANCEL_BOOKING,
            {"booking_id": "opaque"},
            NOW,
        )


def test_stage_pending_action_preserves_optional_expiry() -> None:
    expires_at = NOW + timedelta(minutes=10)

    staged = stage_pending_action(
        None,
        PendingActionType.RESCHEDULE_BOOKING,
        {"booking_id": "opaque"},
        NOW,
        expires_at,
    )

    assert staged.expires_at == expires_at


def test_invalid_pending_action_type_is_rejected() -> None:
    with pytest.raises(PendingActionError):
        stage_pending_action(
            None,
            cast(PendingActionType, "unsupported"),
            {},
            NOW,
        )


def test_confirm_pending_action_sets_confirmation_timestamp_once() -> None:
    staged = stage_pending_action(
        None,
        PendingActionType.CREATE_BOOKING,
        {"service_code": "electrical"},
        NOW,
    )
    confirmed_at = NOW + timedelta(minutes=1)

    confirmed = confirm_pending_action(staged, confirmed_at)

    assert confirmed.status is PendingActionStatus.CONFIRMED
    assert confirmed.confirmed_at == confirmed_at
    with pytest.raises(PendingActionError):
        confirm_pending_action(confirmed, confirmed_at + timedelta(minutes=1))


def test_confirm_pending_action_requires_awaiting_action() -> None:
    confirmed = stage_pending_action(
        None,
        PendingActionType.CREATE_BOOKING,
        {},
        NOW,
    )
    confirmed = confirm_pending_action(confirmed, NOW)

    with pytest.raises(PendingActionError):
        confirm_pending_action(confirmed, NOW)


def test_clear_pending_action_removes_current_action() -> None:
    staged = stage_pending_action(
        None,
        PendingActionType.CANCEL_BOOKING,
        {"booking_id": "opaque"},
        NOW,
    )

    assert clear_pending_action(staged) is None
    assert clear_pending_action(None) is None

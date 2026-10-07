"""Small pure conversation-control and pending-action state helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from receptionist.domain.enums import (
    ControlMode,
    PendingActionStatus,
    PendingActionType,
)


class InvalidControlTransition(ValueError):
    """Raised when a conversation control-mode transition is not approved."""


class PendingActionError(ValueError):
    """Raised when a pending-action transition violates the current-action rules."""


@dataclass(frozen=True, slots=True)
class PendingActionState:
    """The one current pending action stored directly on a Conversation."""

    type: PendingActionType
    status: PendingActionStatus
    payload: dict[str, object]
    created_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None = None


_ALLOWED_CONTROL_TRANSITIONS = {
    (ControlMode.AI, ControlMode.HANDOFF_PENDING),
    (ControlMode.HANDOFF_PENDING, ControlMode.HUMAN),
    (ControlMode.HANDOFF_PENDING, ControlMode.AI),
    (ControlMode.HUMAN, ControlMode.AI),
}


def transition_control_mode(current: ControlMode, target: ControlMode) -> ControlMode:
    """Apply one of the four approved conversation control transitions."""
    if (current, target) not in _ALLOWED_CONTROL_TRANSITIONS:
        raise InvalidControlTransition(f"unsupported control transition: {current} -> {target}")
    return target


def stage_pending_action(
    current: PendingActionState | None,
    action_type: PendingActionType,
    payload: dict[str, object],
    created_at: datetime,
    expires_at: datetime | None = None,
) -> PendingActionState:
    """Stage one supported action when the Conversation has no current action."""
    if current is not None:
        raise PendingActionError("a pending action already exists")
    if action_type not in {
        PendingActionType.CREATE_BOOKING,
        PendingActionType.RESCHEDULE_BOOKING,
        PendingActionType.CANCEL_BOOKING,
    }:
        raise PendingActionError("unsupported pending action type")
    return PendingActionState(
        type=action_type,
        status=PendingActionStatus.AWAITING_CONFIRMATION,
        payload=dict(payload),
        created_at=created_at,
        confirmed_at=None,
        expires_at=expires_at,
    )


def confirm_pending_action(
    current: PendingActionState, confirmed_at: datetime
) -> PendingActionState:
    """Confirm an awaiting action exactly once."""
    if current.status is not PendingActionStatus.AWAITING_CONFIRMATION:
        raise PendingActionError("pending action is not awaiting confirmation")
    return replace(
        current,
        status=PendingActionStatus.CONFIRMED,
        confirmed_at=confirmed_at,
    )


def clear_pending_action(current: PendingActionState | None) -> None:
    """Clear the one current action without creating action history."""
    return None

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from receptionist.application.booking import (
    ToolExecutionClaimOutcome,
    _claim_tool_execution,
)
from receptionist.db.models import Contact, Conversation, ToolExecution
from receptionist.domain.enums import (
    ControlMode,
    ConversationChannel,
    ConversationLanguageMode,
    ConversationStatus,
    ToolExecutionStatus,
)

pytestmark = pytest.mark.integration


async def _conversation_ids(session_factory, count: int = 1) -> tuple[uuid.UUID, ...]:
    async with session_factory.begin() as session:
        ids: list[uuid.UUID] = []
        for _ in range(count):
            contact = Contact(phone_e164=f"+1999{uuid.uuid4().int % 10**10:010d}")
            session.add(contact)
            await session.flush()
            conversation = Conversation(
                contact_id=contact.id,
                channel=ConversationChannel.PHONE,
                status=ConversationStatus.OPEN,
                control_mode=ControlMode.AI,
                language_mode=ConversationLanguageMode.UNKNOWN,
            )
            session.add(conversation)
            await session.flush()
            ids.append(conversation.id)
        return tuple(ids)


def _finish_success(claim) -> None:
    claim.execution.status = ToolExecutionStatus.SUCCEEDED
    claim.execution.sanitized_result = {"ok": True, "operation": claim.execution.tool_name}
    claim.execution.finished_at = datetime.now(UTC)


async def test_new_tool_execution_claim_creates_one_row(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"claim-{uuid.uuid4()}"

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments={"service_code": "plumbing"},
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW
        assert claim.execution.status is ToolExecutionStatus.STARTED
        _finish_success(claim)

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ToolExecution).where(ToolExecution.idempotency_key == key)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].status is ToolExecutionStatus.SUCCEEDED


@pytest.mark.parametrize(
    "status,outcome",
    [
        (ToolExecutionStatus.SUCCEEDED, ToolExecutionClaimOutcome.SUCCEEDED_REPLAY),
        (ToolExecutionStatus.REJECTED, ToolExecutionClaimOutcome.REJECTED_REPLAY),
        (ToolExecutionStatus.FAILED, ToolExecutionClaimOutcome.FAILED_REPLAY),
    ],
)
async def test_terminal_tool_execution_claims_replay_stored_result(
    session_factory, status: ToolExecutionStatus, outcome: ToolExecutionClaimOutcome
) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"terminal-{uuid.uuid4()}"

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=key,
            sanitized_arguments={"booking_id": "booking-id"},
        )
        claim.execution.status = status
        claim.execution.sanitized_result = {"ok": status is ToolExecutionStatus.SUCCEEDED}
        claim.execution.safe_error_code = (
            "booking_state_conflict" if status is not ToolExecutionStatus.SUCCEEDED else None
        )
        claim.execution.finished_at = datetime.now(UTC)

    async with session_factory.begin() as session:
        replay = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_cancel_booking",
            idempotency_key=key,
            sanitized_arguments={"booking_id": "booking-id"},
        )
        assert replay.outcome is outcome
        assert replay.execution.sanitized_result == {
            "ok": status is ToolExecutionStatus.SUCCEEDED
        }


async def test_started_tool_execution_claim_is_recoverable_by_same_key(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"started-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id", "expected_state_fingerprint": "fingerprint"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        recovery = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        assert recovery.outcome is ToolExecutionClaimOutcome.STARTED_RECOVERY
        assert recovery.execution.status is ToolExecutionStatus.STARTED


@pytest.mark.parametrize("mismatch", ["tool", "conversation", "arguments"])
async def test_reusing_key_for_different_logical_identity_is_conflict(
    session_factory, mismatch: str
) -> None:
    conversation_id, other_conversation_id = await _conversation_ids(session_factory, count=2)
    key = f"mismatch-{uuid.uuid4()}"
    arguments = {"service_code": "plumbing"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="prepare_create_booking",
            idempotency_key=key,
            sanitized_arguments=arguments,
        )
        _finish_success(claim)

    async with session_factory.begin() as session:
        conflict = await _claim_tool_execution(
            session,
            conversation_id=(
                other_conversation_id if mismatch == "conversation" else conversation_id
            ),
            tool_name=(
                "prepare_cancel_booking" if mismatch == "tool" else "prepare_create_booking"
            ),
            idempotency_key=key,
            sanitized_arguments=(
                {"service_code": "electrical"} if mismatch == "arguments" else arguments
            ),
        )
        assert conflict.outcome is ToolExecutionClaimOutcome.IDEMPOTENCY_CONFLICT
        assert await session.scalar(select(func.count()).select_from(ToolExecution)) >= 1


async def test_started_execution_blocks_a_different_key_for_same_operation(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    first_key = f"active-{uuid.uuid4()}"
    second_key = f"active-{uuid.uuid4()}"
    arguments = {"booking_id": "booking-id"}

    async with session_factory.begin() as session:
        claim = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=first_key,
            sanitized_arguments=arguments,
        )
        assert claim.outcome is ToolExecutionClaimOutcome.NEW

    async with session_factory.begin() as session:
        blocked = await _claim_tool_execution(
            session,
            conversation_id=conversation_id,
            tool_name="confirm_booking_action",
            idempotency_key=second_key,
            sanitized_arguments=arguments,
        )
        assert blocked.outcome is ToolExecutionClaimOutcome.OPERATION_IN_PROGRESS
        assert await session.scalar(select(func.count()).select_from(ToolExecution)) >= 1


async def test_same_key_contention_creates_one_row_and_loser_replays(session_factory) -> None:
    (conversation_id,) = await _conversation_ids(session_factory)
    key = f"contention-{uuid.uuid4()}"
    barrier = asyncio.Barrier(2)

    async def contender():
        async with session_factory.begin() as session:
            await barrier.wait()
            claim = await _claim_tool_execution(
                session,
                conversation_id=conversation_id,
                tool_name="prepare_create_booking",
                idempotency_key=key,
                sanitized_arguments={"service_code": "plumbing"},
            )
            if claim.outcome is ToolExecutionClaimOutcome.NEW:
                _finish_success(claim)
            return claim.outcome

    outcomes = await asyncio.gather(contender(), contender())
    assert sorted(outcome.value for outcome in outcomes) == ["new", "succeeded_replay"]

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(ToolExecution.idempotency_key == key)
            )
            == 1
        )

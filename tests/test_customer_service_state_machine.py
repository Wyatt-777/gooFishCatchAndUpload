"""CS-0 tests for customer-service contracts and state transitions."""

import pytest

from xianyu_assistant.customer_service.models import (
    CustomerServiceConfig,
    ReceptionMode,
    ReceptionStatus,
    ReplyJobStatus,
)
from xianyu_assistant.customer_service.state_machine import (
    CustomerServiceTransitionError,
    transition_reception,
    transition_reply,
    validate_reception_transition,
    validate_reply_transition,
)


def test_customer_service_config_uses_the_v1_defaults() -> None:
    config = CustomerServiceConfig()

    assert config.mode is ReceptionMode.HUMAN_CONFIRMATION
    assert config.poll_interval_seconds == 2
    assert config.max_conversations_per_poll == 10
    assert config.debounce_seconds == 3


def test_reception_session_can_start_stop_and_halt_then_restart() -> None:
    state = ReceptionStatus.STOPPED
    for target in (
        ReceptionStatus.STARTING,
        ReceptionStatus.RUNNING,
        ReceptionStatus.HALTED,
        ReceptionStatus.STARTING,
        ReceptionStatus.RUNNING,
        ReceptionStatus.STOPPING,
        ReceptionStatus.STOPPED,
    ):
        state = transition_reception(state, target)

    assert state is ReceptionStatus.STOPPED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ReceptionStatus.STOPPED, ReceptionStatus.RUNNING),
        (ReceptionStatus.RUNNING, ReceptionStatus.STOPPED),
        (ReceptionStatus.STOPPING, ReceptionStatus.RUNNING),
        (ReceptionStatus.HALTED, ReceptionStatus.RUNNING),
    ],
)
def test_reception_state_machine_rejects_illegal_transitions(
    current: ReceptionStatus, target: ReceptionStatus
) -> None:
    with pytest.raises(CustomerServiceTransitionError):
        validate_reception_transition(current, target)


def test_reply_job_can_follow_manual_review_path() -> None:
    state = ReplyJobStatus.OBSERVED
    for target in (
        ReplyJobStatus.DEBOUNCING,
        ReplyJobStatus.READY,
        ReplyJobStatus.READING_CONTEXT,
        ReplyJobStatus.GENERATING,
        ReplyJobStatus.POLICY_CHECK,
        ReplyJobStatus.AWAITING_REVIEW,
        ReplyJobStatus.SENDING,
        ReplyJobStatus.SENT,
    ):
        state = transition_reply(state, target)

    assert state is ReplyJobStatus.SENT


def test_send_not_performed_can_return_to_manual_review() -> None:
    state = transition_reply(ReplyJobStatus.SENDING, ReplyJobStatus.AWAITING_REVIEW)

    assert state is ReplyJobStatus.AWAITING_REVIEW


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ReplyJobStatus.OBSERVED, ReplyJobStatus.SENDING),
        (ReplyJobStatus.GENERATING, ReplyJobStatus.SENDING),
        (ReplyJobStatus.SENDING, ReplyJobStatus.SUPERSEDED),
        (ReplyJobStatus.SENT, ReplyJobStatus.FAILED),
        (ReplyJobStatus.HANDOFF, ReplyJobStatus.SENDING),
    ],
)
def test_reply_state_machine_rejects_illegal_transitions(
    current: ReplyJobStatus, target: ReplyJobStatus
) -> None:
    with pytest.raises(CustomerServiceTransitionError):
        validate_reply_transition(current, target)

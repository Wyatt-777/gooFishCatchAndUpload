"""Pure state transition rules for customer-service reception."""

from __future__ import annotations

from xianyu_assistant.customer_service.models import ReceptionStatus, ReplyJobStatus


class CustomerServiceTransitionError(ValueError):
    """Raised when a reception or reply job takes an illegal transition."""


_RECEPTION_TRANSITIONS: dict[ReceptionStatus, frozenset[ReceptionStatus]] = {
    ReceptionStatus.STOPPED: frozenset({ReceptionStatus.STARTING}),
    ReceptionStatus.STARTING: frozenset(
        {ReceptionStatus.RUNNING, ReceptionStatus.STOPPING, ReceptionStatus.HALTED}
    ),
    ReceptionStatus.RUNNING: frozenset({ReceptionStatus.STOPPING, ReceptionStatus.HALTED}),
    ReceptionStatus.STOPPING: frozenset({ReceptionStatus.STOPPED}),
    ReceptionStatus.HALTED: frozenset({ReceptionStatus.STARTING}),
}

_REPLY_JOB_TRANSITIONS: dict[ReplyJobStatus, frozenset[ReplyJobStatus]] = {
    ReplyJobStatus.OBSERVED: frozenset(
        {ReplyJobStatus.DEBOUNCING, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.DEBOUNCING: frozenset(
        {ReplyJobStatus.READY, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.READY: frozenset(
        {ReplyJobStatus.READING_CONTEXT, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.READING_CONTEXT: frozenset(
        {ReplyJobStatus.GENERATING, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.GENERATING: frozenset(
        {ReplyJobStatus.POLICY_CHECK, ReplyJobStatus.HANDOFF, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.POLICY_CHECK: frozenset(
        {
            ReplyJobStatus.AWAITING_REVIEW,
            ReplyJobStatus.SENDING,
            ReplyJobStatus.HANDOFF,
            ReplyJobStatus.SUPERSEDED,
            ReplyJobStatus.FAILED,
        }
    ),
    ReplyJobStatus.AWAITING_REVIEW: frozenset(
        {ReplyJobStatus.SENDING, ReplyJobStatus.SUPERSEDED, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.SENDING: frozenset(
        {ReplyJobStatus.AWAITING_REVIEW, ReplyJobStatus.SENT, ReplyJobStatus.FAILED}
    ),
    ReplyJobStatus.SENT: frozenset(),
    ReplyJobStatus.HANDOFF: frozenset(),
    ReplyJobStatus.SUPERSEDED: frozenset(),
    ReplyJobStatus.FAILED: frozenset(),
}


def validate_reception_transition(current: ReceptionStatus, target: ReceptionStatus) -> None:
    """Raise unless a reception session can safely enter ``target``."""
    if target not in _RECEPTION_TRANSITIONS[current]:
        raise CustomerServiceTransitionError(
            f"接待状态不能从 {current.value} 变更为 {target.value}。"
        )


def transition_reception(current: ReceptionStatus, target: ReceptionStatus) -> ReceptionStatus:
    """Validate and return the target reception state."""
    validate_reception_transition(current, target)
    return target


def validate_reply_transition(current: ReplyJobStatus, target: ReplyJobStatus) -> None:
    """Raise unless a reply job can safely enter ``target``."""
    if target not in _REPLY_JOB_TRANSITIONS[current]:
        raise CustomerServiceTransitionError(
            f"回复任务状态不能从 {current.value} 变更为 {target.value}。"
        )


def transition_reply(current: ReplyJobStatus, target: ReplyJobStatus) -> ReplyJobStatus:
    """Validate and return the target reply-job state."""
    validate_reply_transition(current, target)
    return target


def allowed_reception_targets(current: ReceptionStatus) -> frozenset[ReceptionStatus]:
    """Expose an immutable transition view for UI/worker code."""
    return _RECEPTION_TRANSITIONS[current]


def allowed_reply_targets(current: ReplyJobStatus) -> frozenset[ReplyJobStatus]:
    """Expose an immutable transition view for orchestration code."""
    return _REPLY_JOB_TRANSITIONS[current]

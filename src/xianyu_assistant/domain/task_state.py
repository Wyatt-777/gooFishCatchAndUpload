"""Task state-machine validation."""

from xianyu_assistant.domain.models import TaskStatus


class TaskTransitionError(ValueError):
    """Raised when a task is moved through an invalid lifecycle transition."""


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.WAITING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED: frozenset(),
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise a clear error unless a task is allowed to enter ``target``."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise TaskTransitionError(f"任务状态不能从 {current} 变更为 {target}。")

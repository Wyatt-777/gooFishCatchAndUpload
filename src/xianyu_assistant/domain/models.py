"""Data objects shared by task, crawler, persistence, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle states for a keyword collection task."""

    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProductAttribute:
    """One source-side item attribute, such as ``成色：全新``.

    The detail page exposes these values independently of its free-form item
    description.  Keeping the label and value separate lets the publish flow
    show exactly what was collected instead of trying to reconstruct a value
    from the description later.
    """

    name: str
    value: str


TASK_STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.WAITING: "等待",
    TaskStatus.RUNNING: "运行",
    TaskStatus.PAUSED: "暂停",
    TaskStatus.COMPLETED: "完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "取消",
}


@dataclass(frozen=True, slots=True)
class CollectionTask:
    """A user-created keyword collection task persisted in SQLite."""

    id: int
    keyword: str
    status: TaskStatus
    progress: int
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ProductCandidate:
    """A normalized product extracted from its source pages."""

    external_id: str
    title: str
    price: str
    description: str
    category: str
    url: str
    image_urls: tuple[str, ...]
    category_path: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    attributes: tuple[ProductAttribute, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductRecord:
    """A collected product together with its locally persisted identifier."""

    id: int
    task_id: int
    external_id: str
    title: str
    price: str
    description: str
    category: str
    url: str
    image_urls: tuple[str, ...]
    image_paths: tuple[str | None, ...]
    category_path: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    attributes: tuple[ProductAttribute, ...] = ()

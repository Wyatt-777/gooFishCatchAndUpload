"""Browsable list of product collections stored in the local SQLite cache."""

from __future__ import annotations

from collections.abc import Iterable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from xianyu_assistant.domain.models import TASK_STATUS_LABELS, CollectionTask


class CollectionHistoryPanel(QWidget):
    """Let a user reopen products from a previously completed collection."""

    task_open_requested = pyqtSignal(int)
    refresh_requested = pyqtSignal()

    _HEADERS = ("采集时间", "卖家主页", "状态", "商品数", "操作")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("collectionHistory")

        title = QLabel("历史抓取")
        title.setObjectName("historyTitle")
        subtitle = QLabel("历史记录保存在本机；打开任一批次即可继续查看、下载图片或发布辅助。")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        self.refresh_button = QPushButton("刷新记录")
        self.refresh_button.setObjectName("secondaryAction")

        header_text = QVBoxLayout()
        header_text.setSpacing(3)
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        header = QHBoxLayout()
        header.addLayout(header_text)
        header.addStretch()
        header.addWidget(self.refresh_button, alignment=Qt.AlignmentFlag.AlignTop)

        self.table = QTableWidget(0, len(self._HEADERS))
        self.table.setHorizontalHeaderLabels(self._HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        headers = self.table.horizontalHeader()
        headers.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        headers.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        headers.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        headers.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        headers.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        self.empty_state = self._build_empty_state()
        content = QWidget()
        self._content_layout = QStackedLayout(content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.addWidget(self.empty_state)
        self._content_layout.addWidget(self.table)
        self._content_layout.setCurrentWidget(self.empty_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(content, 1)

        self.refresh_button.clicked.connect(self.refresh_requested)
        self.table.cellDoubleClicked.connect(self._open_row)

    def set_tasks(self, tasks: Iterable[CollectionTask]) -> None:
        """Render collections newest first, as supplied by the repository."""

        task_list = list(tasks)
        self.table.setRowCount(0)
        if not task_list:
            self._content_layout.setCurrentWidget(self.empty_state)
            return

        for task in task_list:
            self._add_task_row(task)
        self._content_layout.setCurrentWidget(self.table)

    def _build_empty_state(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("emptyState")
        layout = QVBoxLayout(frame)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge = QLabel("暂无记录")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setObjectName("emptyStateBadge")
        title = QLabel("还没有历史抓取记录")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("emptyStateTitle")
        hint = QLabel("完成一次卖家主页采集后，记录会自动保存在这里。")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setObjectName("mutedText")
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)
        return frame

    def _add_task_row(self, task: CollectionTask) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = (
            task.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            task.keyword,
            TASK_STATUS_LABELS[task.status],
            f"{task.progress} 件",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in {0, 2, 3}:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, column, item)

        open_button = QPushButton("查看内容")
        open_button.setObjectName("linkAction")
        open_button.setEnabled(task.progress > 0)
        open_button.setToolTip("打开该批次已采集的商品")
        open_button.clicked.connect(
            lambda _checked=False, collection_id=task.id: self.task_open_requested.emit(collection_id)
        )
        self.table.setCellWidget(row, len(values), open_button)

    def _open_row(self, row: int, _column: int) -> None:
        """Support double-clicking any task row in addition to the explicit button."""

        button = self.table.cellWidget(row, len(self._HEADERS) - 1)
        if isinstance(button, QPushButton) and button.isEnabled():
            button.click()

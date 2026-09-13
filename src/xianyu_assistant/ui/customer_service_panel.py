"""Qt reception panel for locally generated customer-service drafts."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from PyQt6.QtCore import QSignalBlocker, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from xianyu_assistant.browser.manager import BrowserConnectionConfig, BrowserManager
from xianyu_assistant.customer_service.browser_adapter import (
    REAL_XIANYU_CHAT_CONTRACT,
    PlaywrightXianyuChatAdapter,
)
from xianyu_assistant.customer_service.customer_service_worker import (
    CustomerServiceWorker,
    CustomerServiceWorkerError,
)
from xianyu_assistant.customer_service.deepseek_client import DeepSeekClient
from xianyu_assistant.customer_service.models import (
    CustomerServiceConfig,
    PriceChangeDraft,
    PriceChangeStatus,
    ReceptionMode,
    ReceptionStatus,
    ReplyDraft,
    ReplyJobStatus,
)
from xianyu_assistant.customer_service.price_change import PricePolicyError, check_price_change
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import CredentialStore, KeyringCredentialStore


class CustomerServiceRunThread(QThread):
    """Own the synchronous Playwright and customer-service worker resources."""

    status_changed = pyqtSignal(str)
    drafts_changed = pyqtSignal()
    run_error = pyqtSignal(str)
    approval_finished = pyqtSignal(str, bool, str)
    price_change_finished = pyqtSignal(str, bool, str)

    def __init__(
        self,
        repository: CustomerServiceRepository,
        browser_config: BrowserConnectionConfig,
        *,
        mode: ReceptionMode = ReceptionMode.HUMAN_CONFIRMATION,
        credential_store: CredentialStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._browser_config = browser_config
        self._mode = mode
        self._credential_store = credential_store
        self._engine: CustomerServiceWorker | None = None
        self._engine_lock = threading.RLock()
        self._stop_requested = threading.Event()

    def run(self) -> None:
        """Attach, health-check, and poll until the UI asks the run to stop."""
        manager = BrowserManager(self._browser_config)
        try:
            manager.connect()
            settings = _deepseek_settings(self._repository)
            credential_store = self._credential_store or KeyringCredentialStore()
            model = DeepSeekClient(settings, credential_store)
            adapter = PlaywrightXianyuChatAdapter(manager, REAL_XIANYU_CHAT_CONTRACT)
            engine = CustomerServiceWorker(
                adapter,
                self._repository,
                model,
                config=CustomerServiceConfig(mode=self._mode),
                text_model=settings.text_model,
                vision_model=settings.vision_model,
            )
            with self._engine_lock:
                self._engine = engine
            status = engine.start()
            self.status_changed.emit(status.value)
            if status is not ReceptionStatus.RUNNING:
                return
            while not self._stop_requested.is_set() and engine.status is ReceptionStatus.RUNNING:
                engine.run_once()
                self.drafts_changed.emit()
                self._stop_requested.wait(CustomerServiceConfig().poll_interval_seconds)
            if engine.status is ReceptionStatus.RUNNING:
                engine.stop("用户停止")
            self.status_changed.emit(engine.status.value)
        except Exception as error:  # noqa: BLE001 - surface a redacted, bounded UI error
            self.run_error.emit(_safe_error_message(error))
            self.status_changed.emit(ReceptionStatus.HALTED.value)
        finally:
            with self._engine_lock:
                self._engine = None
            manager.disconnect()

    def request_stop(self) -> None:
        """Interrupt the poll wait and ask the engine to stop safely."""
        self._stop_requested.set()
        with self._engine_lock:
            engine = self._engine
        if engine is not None:
            engine.request_stop("用户停止")

    def drafts_snapshot(self) -> list[ReplyDraft]:
        """Return the thread-safe in-memory draft view for the Qt table."""
        with self._engine_lock:
            engine = self._engine
        return [] if engine is None else engine.drafts.list()

    def edit_draft(self, job_id: str, text: str) -> ReplyDraft:
        """Edit a draft through the worker's validation boundary."""
        with self._engine_lock:
            engine = self._engine
        if engine is None:
            raise CustomerServiceWorkerError("客服接待尚未运行。")
        return engine.edit_draft(job_id, text)

    def approve_draft(self, job_id: str, text: str) -> None:
        """Queue a user-approved send without blocking the Qt UI thread."""
        with self._engine_lock:
            engine = self._engine
        if engine is None:
            raise CustomerServiceWorkerError("客服接待尚未运行。")
        future = engine.enqueue_approval(job_id, text)

        def completed(result_future: object) -> None:
            try:
                result = result_future.result()  # type: ignore[attr-defined]
                message = "消息已发送并完成页面回读确认。" if result.sent else (
                    result.reason or "消息未发送。"
                )
                self.approval_finished.emit(job_id, result.sent, message)
            except Exception as error:  # noqa: BLE001 - cross-thread UI boundary
                self.approval_finished.emit(job_id, False, _safe_error_message(error))

        future.add_done_callback(completed)

    def approve_price_change(self, task_id: str) -> None:
        """Queue a user-confirmed real-order price change on the browser thread."""
        with self._engine_lock:
            engine = self._engine
        if engine is None:
            raise CustomerServiceWorkerError("客服接待尚未运行。")
        future = engine.enqueue_price_change(task_id)

        def completed(result_future: object) -> None:
            try:
                result = result_future.result()  # type: ignore[attr-defined]
                message = (
                    f"订单价格已修改为 ¥{result.receipt.applied_price}。"
                    if result.applied and result.receipt is not None
                    else result.reason or "订单价格未修改。"
                )
                self.price_change_finished.emit(task_id, result.applied, message)
            except Exception as error:  # noqa: BLE001 - cross-thread UI boundary
                self.price_change_finished.emit(task_id, False, _safe_error_message(error))

        future.add_done_callback(completed)


class CustomerServicePanel(QWidget):
    """Run guarded human-review or explicitly confirmed automatic reception."""

    status_changed = pyqtSignal(str)

    def __init__(
        self,
        repository: CustomerServiceRepository,
        browser_config_provider: Callable[[], BrowserConnectionConfig],
        *,
        credential_store: CredentialStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self._browser_config_provider = browser_config_provider
        self._credential_store = credential_store
        self._run_thread: CustomerServiceRunThread | None = None
        self._selected_job_id: str | None = None
        self._selected_price_task_id: str | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1_000)
        self._refresh_timer.timeout.connect(self.refresh_drafts)
        self.setObjectName("customerServicePanel")
        self._build_ui()
        self.refresh_handoffs()
        self.refresh_price_changes()

    def _build_ui(self) -> None:
        title = QLabel("客服接待")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "审核回复，或全自动回复并处理合规改价；异常会话单独转人工，其他顾客照常处理。"
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("pageSubtitle")

        control_group = QGroupBox("运行控制")
        control_group.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        control_layout = QVBoxLayout(control_group)
        control_layout.setSpacing(7)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("人工审核草稿", ReceptionMode.HUMAN_CONFIRMATION)
        self.mode_combo.addItem(
            "全自动（回复与改价，启动时需确认）",
            ReceptionMode.AUTO_SEND,
        )
        self.mode_combo.setToolTip(
            "默认人工审核；全自动模式会发送回复，并对通过全部规则的待付款订单直接改价。"
        )
        self.status_label = QLabel("已停止")
        self.status_label.setObjectName("receptionStatus")
        self.status_label.setProperty("tone", "neutral")
        self.status_label.setFixedHeight(32)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.start_button = QPushButton("启动客服接待")
        self.start_button.setObjectName("primaryAction")
        self.stop_button = QPushButton("停止接待")
        self.stop_button.setEnabled(False)
        self.connection_hint = QLabel("浏览器连接：使用“浏览器设置”中的本机 CDP 端口")
        self.connection_hint.setObjectName("mutedText")
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("运行模式"))
        controls.addWidget(self.mode_combo, 1)
        controls.addSpacing(8)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(QLabel("接待状态"))
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.connection_hint)
        control_layout.addLayout(controls)
        control_layout.addLayout(status_row)

        self.price_change_group = QGroupBox("订单改价 · 审核与记录")
        price_layout = QVBoxLayout(self.price_change_group)
        price_layout.setSpacing(7)
        self.price_hint = QLabel(
            "人工模式点击确认；全自动模式会复核会话、型号和价格后执行，失败不重试。"
        )
        self.price_hint.setWordWrap(True)
        self.price_hint.setObjectName("sectionHint")
        self.price_filter_combo = QComboBox()
        self.price_filter_combo.addItem("需要处理", "attention")
        self.price_filter_combo.addItem("全部记录", "all")
        self.price_filter_combo.addItem("已完成", "completed")
        self.price_filter_combo.setToolTip("默认隐藏已完成记录，审计数据仍保存在本地。")
        self.price_filter_combo.setMinimumWidth(112)
        price_summary = QHBoxLayout()
        price_summary.setSpacing(8)
        price_summary.addWidget(self.price_hint, 1)
        price_summary.addWidget(QLabel("显示"))
        price_summary.addWidget(self.price_filter_combo)
        self.price_change_table = QTableWidget(0, 5)
        self.price_change_table.setHorizontalHeaderLabels(
            ("会话", "商品", "顾客报价", "建议改价", "状态")
        )
        self.price_change_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.price_change_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.price_change_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.price_change_table.setAlternatingRowColors(True)
        self.price_change_table.setShowGrid(False)
        self.price_change_table.verticalHeader().setVisible(False)
        price_header = self.price_change_table.horizontalHeader()
        price_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        price_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        price_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        price_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        price_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.price_change_table.setColumnWidth(0, 130)
        self.price_change_table.setFixedHeight(96)
        self.price_change_empty_state = self._build_table_empty_state(
            "暂无需要处理的改价",
            "已完成记录可从右上角筛选器查看。",
        )
        self.price_change_empty_state.setFixedHeight(96)
        price_content = QWidget()
        price_content.setFixedHeight(96)
        self.price_change_content_layout = QStackedLayout(price_content)
        self.price_change_content_layout.setContentsMargins(0, 0, 0, 0)
        self.price_change_content_layout.addWidget(self.price_change_empty_state)
        self.price_change_content_layout.addWidget(self.price_change_table)
        self.price_change_content_layout.setCurrentWidget(self.price_change_empty_state)
        self.price_change_detail = QLabel("选择一条改价建议后查看最低价与生成依据。")
        self.price_change_detail.setWordWrap(True)
        self.price_change_detail.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.price_change_detail.setFixedHeight(36)
        self.price_change_detail.setObjectName("sectionHint")
        self.confirm_price_change_button = QPushButton("确认修改实际订单价格")
        self.confirm_price_change_button.setEnabled(False)
        self.confirm_price_change_button.setToolTip(
            "执行前会重读会话、复核顾客最新消息和商品最低成交价。"
        )
        self.delete_price_change_button = QPushButton("删除所选记录")
        self.delete_price_change_button.setObjectName("dangerAction")
        self.delete_price_change_button.setEnabled(False)
        self.delete_price_change_button.setToolTip(
            "只删除程序中的本地改价记录，不会撤销或再次操作闲鱼订单。"
        )
        price_actions = QHBoxLayout()
        price_actions.addWidget(self.confirm_price_change_button)
        price_actions.addWidget(self.delete_price_change_button)
        price_actions.addStretch()
        price_layout.addLayout(price_summary)
        price_layout.addWidget(price_content)
        price_layout.addWidget(self.price_change_detail)
        price_layout.addLayout(price_actions)

        self.draft_table = QTableWidget(0, 4)
        self.draft_table.setHorizontalHeaderLabels(("会话", "状态", "草稿预览", "任务 ID"))
        self.draft_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.draft_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.draft_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.draft_table.setAlternatingRowColors(True)
        self.draft_table.setShowGrid(False)
        self.draft_table.verticalHeader().setVisible(False)
        draft_header = self.draft_table.horizontalHeader()
        draft_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        draft_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        draft_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.draft_table.setColumnWidth(0, 132)
        self.draft_table.setColumnWidth(1, 96)
        self.draft_table.setColumnHidden(3, True)
        self.draft_table.setMinimumHeight(170)
        self.draft_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        self.draft_group = QGroupBox("回复草稿与状态")
        draft_layout = QVBoxLayout(self.draft_group)
        self.draft_empty_state = self._build_table_empty_state(
            "暂无待处理草稿",
            "启动客服接待后，新消息的回复草稿会显示在这里。",
        )
        draft_content = QWidget()
        self.draft_content_layout = QStackedLayout(draft_content)
        self.draft_content_layout.setContentsMargins(0, 0, 0, 0)
        self.draft_content_layout.addWidget(self.draft_empty_state)
        self.draft_content_layout.addWidget(self.draft_table)
        self.draft_content_layout.setCurrentWidget(self.draft_empty_state)
        draft_layout.addWidget(draft_content)

        self.handoff_group = QGroupBox("转人工通知")
        handoff_layout = QVBoxLayout(self.handoff_group)
        self.handoff_hint = QLabel(
            "程序内提醒 · 暂无待处理通知\n其他顾客的接待不会受到影响。"
        )
        self.handoff_hint.setWordWrap(True)
        self.handoff_hint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.handoff_hint.setMaximumHeight(52)
        self.handoff_hint.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.handoff_hint.setObjectName("handoffSummary")
        self.handoff_table = QTableWidget(0, 3)
        self.handoff_table.setHorizontalHeaderLabels(("会话", "转人工原因", "触发时间"))
        self.handoff_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.handoff_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.handoff_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.handoff_table.setAlternatingRowColors(True)
        self.handoff_table.setShowGrid(False)
        self.handoff_table.verticalHeader().setVisible(False)
        handoff_header = self.handoff_table.horizontalHeader()
        handoff_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        handoff_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        handoff_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.handoff_table.setColumnWidth(0, 132)
        self.handoff_table.setMinimumHeight(140)
        self.handoff_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        self.resolve_handoff_button = QPushButton("已处理，恢复该顾客")
        self.resolve_handoff_button.setEnabled(False)
        handoff_actions = QHBoxLayout()
        handoff_actions.addWidget(self.resolve_handoff_button)
        handoff_actions.addStretch()
        self.handoff_empty_state = self._build_table_empty_state(
            "暂无转人工通知",
            "安全、售后或争议信息触发后会在这里提醒。",
        )
        handoff_content = QWidget()
        self.handoff_content_layout = QStackedLayout(handoff_content)
        self.handoff_content_layout.setContentsMargins(0, 0, 0, 0)
        self.handoff_content_layout.addWidget(self.handoff_empty_state)
        self.handoff_content_layout.addWidget(self.handoff_table)
        self.handoff_content_layout.setCurrentWidget(self.handoff_empty_state)
        handoff_layout.addWidget(self.handoff_hint)
        handoff_layout.addWidget(handoff_content)
        handoff_layout.addLayout(handoff_actions)

        edit_group = QGroupBox("草稿编辑与复核")
        edit_layout = QVBoxLayout(edit_group)
        self.selected_hint = QLabel("请选择一条待审核草稿。")
        self.selected_hint.setObjectName("sectionHint")
        self.draft_editor = QPlainTextEdit()
        self.draft_editor.setPlaceholderText("生成草稿后可在这里编辑；保存只更新本地草稿。")
        self.draft_editor.setMinimumHeight(100)
        self.draft_editor.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        self.save_edit_button = QPushButton("保存草稿编辑")
        self.save_edit_button.setEnabled(False)
        self.confirm_button = QPushButton("确认并发送")
        self.confirm_button.setEnabled(False)
        self.confirm_button.setToolTip("发送前会重新读取会话；有新消息时自动废弃旧草稿。")
        edit_buttons = QHBoxLayout()
        edit_buttons.addWidget(self.save_edit_button)
        edit_buttons.addWidget(self.confirm_button)
        edit_buttons.addStretch()
        edit_layout.addWidget(self.selected_hint)
        edit_layout.addWidget(self.draft_editor)
        edit_layout.addLayout(edit_buttons)

        draft_workspace = QWidget()
        draft_workspace_layout = QVBoxLayout(draft_workspace)
        draft_workspace_layout.setContentsMargins(0, 0, 0, 0)
        draft_workspace_layout.setSpacing(10)
        draft_workspace_layout.addWidget(self.draft_group, 3)
        draft_workspace_layout.addWidget(edit_group, 2)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("customerServiceWorkspace")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        self.workspace_splitter.setOpaqueResize(False)
        left_workspace = QWidget()
        left_workspace_layout = QVBoxLayout(left_workspace)
        left_workspace_layout.setContentsMargins(0, 0, 0, 0)
        left_workspace_layout.setSpacing(10)
        left_workspace_layout.addWidget(self.price_change_group)
        left_workspace_layout.addWidget(self.handoff_group, 1)
        self.workspace_splitter.addWidget(left_workspace)
        self.workspace_splitter.addWidget(draft_workspace)
        self.workspace_splitter.setStretchFactor(0, 2)
        self.workspace_splitter.setStretchFactor(1, 3)
        self.workspace_splitter.setSizes((420, 680))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 11, 20, 11)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(control_group)
        layout.addWidget(self.workspace_splitter, 1)

        self.start_button.clicked.connect(self.start_reception)
        self.stop_button.clicked.connect(self.stop_reception)
        self.handoff_table.itemSelectionChanged.connect(self._handoff_selected)
        self.resolve_handoff_button.clicked.connect(self.resolve_selected_handoff)
        self.draft_table.itemSelectionChanged.connect(self._draft_selected)
        self.save_edit_button.clicked.connect(self.save_draft_edit)
        self.confirm_button.clicked.connect(self.confirm_selected_draft)
        self.price_change_table.itemSelectionChanged.connect(self._price_change_selected)
        self.confirm_price_change_button.clicked.connect(self.confirm_selected_price_change)
        self.delete_price_change_button.clicked.connect(self.delete_selected_price_change)
        self.price_filter_combo.currentIndexChanged.connect(self.refresh_price_changes)

    @staticmethod
    def _build_table_empty_state(title: str, hint: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("tableEmptyState")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label = QLabel(title)
        title_label.setObjectName("tableEmptyTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_label = QLabel(hint)
        hint_label.setObjectName("mutedText")
        hint_label.setWordWrap(True)
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        layout.addWidget(hint_label)
        return frame

    def start_reception(self) -> None:
        """Start the selected mode, confirming every fully automatic run."""
        if self._run_thread is not None and self._run_thread.isRunning():
            return
        mode = ReceptionMode(self.mode_combo.currentData())
        if mode is ReceptionMode.AUTO_SEND:
            choice = QMessageBox.warning(
                self,
                "确认开启全自动客服",
                "全自动模式会代表你直接回复顾客，并会修改符合规则的真实待付款订单价格。"
                "请确认商品标价、最低成交价和议价规则均已核对。\n\n"
                "改价前会重新读取会话并校验型号、最终成交价、价格区间和最新消息；"
                "无法确认或页面操作失败时不会重试，只会将该顾客转人工，其他顾客继续处理。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice is not QMessageBox.StandardButton.Yes:
                self._set_status("已取消全自动客服，未启动客服接待。")
                return
        try:
            config = self._browser_config_provider()
        except Exception as error:  # noqa: BLE001 - provider is a UI boundary
            self._set_status(f"浏览器配置不可用：{error}", error=True)
            return
        thread = CustomerServiceRunThread(
            self.repository,
            config,
            mode=mode,
            credential_store=self._credential_store,
            parent=self,
        )
        thread.status_changed.connect(self._run_status_changed)
        thread.drafts_changed.connect(self.refresh_drafts)
        thread.run_error.connect(lambda message: self._set_status(message, error=True))
        thread.approval_finished.connect(self._approval_finished)
        price_signal = getattr(thread, "price_change_finished", None)
        if price_signal is not None:
            price_signal.connect(self._price_change_finished)
        thread.finished.connect(self._run_finished)
        self._run_thread = thread
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.mode_combo.setEnabled(False)
        self._set_status(
            "正在启动全自动客服…"
            if mode is ReceptionMode.AUTO_SEND
            else "正在启动客服页…"
        )
        self._refresh_timer.start()
        thread.start()

    def stop_reception(self) -> None:
        """Stop the background run without closing the user's browser."""
        if self._run_thread is None:
            return
        self._set_status("正在停止客服接待…")
        self.stop_button.setEnabled(False)
        self._run_thread.request_stop()

    def refresh_drafts(self) -> None:
        """Refresh only the local draft table; no browser operation is performed."""
        self.refresh_handoffs()
        self.refresh_price_changes()
        if self._run_thread is None:
            return
        drafts = self._run_thread.drafts_snapshot()
        self.draft_group.setTitle(f"回复草稿与状态 · {len(drafts)}")
        selected = self._selected_job_id
        blocker = QSignalBlocker(self.draft_table)
        self.draft_table.setRowCount(0)
        for draft in drafts:
            row = self.draft_table.rowCount()
            self.draft_table.insertRow(row)
            values = (
                _conversation_label(draft.conversation_key),
                _status_label(draft.status),
                draft.failure_reason
                if draft.status in {ReplyJobStatus.FAILED, ReplyJobStatus.HANDOFF}
                else draft.reply_text or "（等待生成）",
                draft.job_id,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 3:
                    item.setData(Qt.ItemDataRole.UserRole, draft.job_id)
                self.draft_table.setItem(row, column, item)
            if draft.job_id == selected:
                self.draft_table.selectRow(row)
        del blocker
        self.draft_content_layout.setCurrentWidget(
            self.draft_table if drafts else self.draft_empty_state
        )
        self._update_edit_state()

    def refresh_price_changes(self) -> None:
        """Refresh persisted price proposals without touching the browser."""
        tasks = self.repository.list_price_change_drafts()
        revalidated: list[PriceChangeDraft] = []
        for task in tasks:
            if task.status is not PriceChangeStatus.NEEDS_CONFIGURATION:
                revalidated.append(task)
                continue
            # Option-adjusted tasks must be rechecked by the worker against the
            # live conversation.  Revalidating them here against the base
            # catalogue would incorrectly remove the option surcharge.
            if task.price_adjustment_key is not None:
                revalidated.append(task)
                continue
            knowledge = self.repository.get_product_knowledge(task.product_key)
            if knowledge is None:
                revalidated.append(task)
                continue
            try:
                decision = check_price_change(task.proposed_price, knowledge)
            except PricePolicyError as error:
                updated = replace(task, failure_reason=str(error))
            else:
                updated = replace(
                    task,
                    proposed_price=decision.approved_price,
                    minimum_price=decision.minimum_price,
                    listed_price=decision.listed_price,
                    status=PriceChangeStatus.AWAITING_REVIEW,
                    failure_reason=None,
                )
            if updated != task:
                self.repository.save_price_change_draft(updated)
            revalidated.append(updated)
        tasks = revalidated
        attention_statuses = {
            PriceChangeStatus.AWAITING_REVIEW,
            PriceChangeStatus.NEEDS_CONFIGURATION,
            PriceChangeStatus.FAILED,
        }
        filter_name = self.price_filter_combo.currentData()
        if filter_name == "completed":
            visible_tasks = [
                task
                for task in tasks
                if task.status in {PriceChangeStatus.APPLIED, PriceChangeStatus.SUPERSEDED}
            ]
        elif filter_name == "all":
            visible_tasks = tasks
        else:
            visible_tasks = [task for task in tasks if task.status in attention_statuses]
        selected = self._selected_price_task_id
        visible_ids = {task.task_id for task in visible_tasks}
        if selected not in visible_ids:
            selected = None
            self._selected_price_task_id = None
        blocker = QSignalBlocker(self.price_change_table)
        self.price_change_table.setRowCount(0)
        for task in visible_tasks:
            row = self.price_change_table.rowCount()
            self.price_change_table.insertRow(row)
            values = (
                _compact_conversation_label(task.conversation_key),
                task.product_name,
                f"¥{task.customer_offer}" if task.customer_offer else "—",
                f"¥{task.proposed_price}",
                _price_change_status_label(task.status),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, task.task_id)
                self.price_change_table.setItem(row, column, item)
            if task.task_id == selected:
                self.price_change_table.selectRow(row)
        del blocker
        attention_count = sum(task.status in attention_statuses for task in tasks)
        self.price_change_group.setTitle(
            f"订单改价 · 审核与记录 · {attention_count} 需处理"
        )
        self.price_change_content_layout.setCurrentWidget(
            self.price_change_table if visible_tasks else self.price_change_empty_state
        )
        self._price_change_selected()

    def confirm_selected_price_change(self) -> None:
        """Require an action-time warning before changing a real order price."""
        task = self._selected_price_change()
        if task is None or task.status is not PriceChangeStatus.AWAITING_REVIEW:
            self._set_status("请先选择一条可执行的改价建议。", error=True)
            return
        warning = (
            "即将修改真实订单价格\n\n"
            f"商品：{task.product_name}\n"
            f"顾客报价：¥{task.customer_offer or '未记录'}\n"
            f"最低成交价：¥{task.minimum_price or '未设置'}\n"
            f"最终改价：¥{task.proposed_price}\n\n"
            "确认后，程序会打开聊天页右上角的“修改价格”，填写金额并单击“确定修改”。"
        )
        choice = QMessageBox.warning(
            self,
            "确认修改实际订单价格",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice is not QMessageBox.StandardButton.Yes:
            self._set_status("已取消，订单价格未修改。")
            return
        if self._run_thread is None or not self._run_thread.isRunning():
            self._set_status("请先启动客服接待，再执行订单改价。", error=True)
            return
        self.confirm_price_change_button.setEnabled(False)
        self._run_thread.approve_price_change(task.task_id)
        self._set_status("正在重读会话、复核最低价并修改订单价格…")

    def delete_selected_price_change(self) -> None:
        """Delete one selected local audit row after explicit confirmation."""
        task = self._selected_price_change()
        if task is None:
            self._set_status("请先选择一条改价记录。", error=True)
            return
        if task.status is PriceChangeStatus.APPLYING:
            self._set_status("该记录正在执行改价，暂时不能删除。", error=True)
            return
        warning = (
            "确认删除这条本地改价记录吗？\n\n"
            f"商品：{task.product_name}\n"
            f"建议改价：¥{task.proposed_price}\n"
            f"状态：{_price_change_status_label(task.status)}\n\n"
            "删除后无法从程序恢复；此操作不会撤销或再次执行闲鱼订单改价。"
        )
        choice = QMessageBox.warning(
            self,
            "删除改价记录",
            warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice is not QMessageBox.StandardButton.Yes:
            return
        if not self.repository.delete_price_change_draft(task.task_id):
            self._set_status("该改价记录已不存在，请刷新后重试。", error=True)
            self.refresh_price_changes()
            return
        self._selected_price_task_id = None
        self.refresh_price_changes()
        self._set_status("本地改价记录已删除；未操作闲鱼订单。")

    def refresh_handoffs(self) -> None:
        """Refresh persisted, non-blocking in-app handoff notifications."""
        selected_event_id = self._selected_handoff_event_id()
        events = self.repository.list_open_handoff_events()
        blocker = QSignalBlocker(self.handoff_table)
        self.handoff_table.setRowCount(0)
        for event in events:
            row = self.handoff_table.rowCount()
            self.handoff_table.insertRow(row)
            values = (
                _compact_conversation_label(event.conversation_key),
                event.reason,
                event.created_at.astimezone().strftime("%m-%d %H:%M"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    event.conversation_key
                    if column == 0
                    else event.reason
                    if column == 1
                    else event.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                )
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, event.event_id)
                self.handoff_table.setItem(row, column, item)
            if event.event_id == selected_event_id:
                self.handoff_table.selectRow(row)
        del blocker
        count = len(events)
        self.handoff_content_layout.setCurrentWidget(
            self.handoff_table if events else self.handoff_empty_state
        )
        self.handoff_group.setTitle(f"转人工通知 · {count}")
        self.handoff_hint.setText(
            f"程序内提醒 · 待处理 {count} 条\n"
            "列表中的顾客暂停自动回复；其他顾客照常接待。"
        )
        self._handoff_selected()

    def resolve_selected_handoff(self) -> None:
        """Release one isolated conversation after the operator handled it."""
        event_id = self._selected_handoff_event_id()
        if event_id is None:
            self._set_status("请先选择一条转人工通知。", error=True)
            return
        if not self.repository.resolve_handoff_event(event_id):
            self._set_status("该转人工通知已处理或不存在。", error=True)
            self.refresh_handoffs()
            return
        self.refresh_handoffs()
        self._set_status("已标记人工处理完成；该顾客后续新消息将恢复自动处理。")

    def save_draft_edit(self) -> None:
        """Persist a validated edit while keeping the draft in review status."""
        if self._run_thread is None or self._selected_job_id is None:
            self._set_status("请先选择一条待审核草稿。", error=True)
            return
        try:
            self._run_thread.edit_draft(self._selected_job_id, self.draft_editor.toPlainText())
        except CustomerServiceWorkerError as error:
            self._set_status(f"草稿未保存：{error}", error=True)
            return
        self.refresh_drafts()
        self._set_status("草稿编辑已保存；未发送任何消息。")

    def confirm_selected_draft(self) -> None:
        """Send the visible editor text after this explicit user action."""
        if self._run_thread is None or self._selected_job_id is None:
            self._set_status("请先选择一条待审核草稿。", error=True)
            return
        try:
            self._run_thread.approve_draft(
                self._selected_job_id,
                self.draft_editor.toPlainText(),
            )
        except CustomerServiceWorkerError as error:
            self._set_status(f"消息未发送：{error}", error=True)
            return
        self.save_edit_button.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self._set_status("正在执行发送前重读与页面发送…")

    def closeEvent(self, event: object) -> None:
        """Stop the owned worker before the panel is destroyed."""
        self.stop_reception()
        if self._run_thread is not None and self._run_thread.isRunning():
            self._run_thread.wait(3_000)
        super().closeEvent(event)  # type: ignore[arg-type]

    def _draft_selected(self) -> None:
        row = self.draft_table.currentRow()
        if row < 0:
            self._selected_job_id = None
            self._update_edit_state()
            return
        job_item = self.draft_table.item(row, 3)
        self._selected_job_id = job_item.data(Qt.ItemDataRole.UserRole) if job_item else None
        draft = next(
            (item for item in self._current_drafts() if item.job_id == self._selected_job_id),
            None,
        )
        if draft is not None:
            self.draft_editor.setPlainText(draft.reply_text)
            self.selected_hint.setText(f"会话：{_conversation_label(draft.conversation_key)} · {_status_label(draft.status)}")
        self._update_edit_state()

    def _selected_handoff_event_id(self) -> int | None:
        row = self.handoff_table.currentRow()
        if row < 0:
            return None
        item = self.handoff_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return int(value) if isinstance(value, int) else None

    def _handoff_selected(self) -> None:
        self.resolve_handoff_button.setEnabled(self._selected_handoff_event_id() is not None)

    def _selected_price_change(self) -> PriceChangeDraft | None:
        if self._selected_price_task_id is None:
            return None
        return next(
            (
                task
                for task in self.repository.list_price_change_drafts()
                if task.task_id == self._selected_price_task_id
            ),
            None,
        )

    def _price_change_selected(self) -> None:
        row = self.price_change_table.currentRow()
        if row < 0:
            self._selected_price_task_id = None
            self.price_change_detail.setText("选择一条改价建议后查看最低价与生成依据。")
            self.price_change_detail.setToolTip("")
            self.confirm_price_change_button.setEnabled(False)
            self.delete_price_change_button.setEnabled(False)
            return
        item = self.price_change_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self._selected_price_task_id = value if isinstance(value, str) else None
        task = self._selected_price_change()
        if task is None:
            self.confirm_price_change_button.setEnabled(False)
            self.delete_price_change_button.setEnabled(False)
            return
        detail = (
            f"最低成交价 ¥{task.minimum_price or '未设置'} · "
            f"{task.rationale or '无生成说明'}"
        )
        if task.failure_reason:
            detail += f" · {task.failure_reason}"
        self.price_change_detail.setText(detail)
        self.price_change_detail.setToolTip(detail)
        self.confirm_price_change_button.setEnabled(
            task.status is PriceChangeStatus.AWAITING_REVIEW
        )
        self.delete_price_change_button.setEnabled(
            task.status is not PriceChangeStatus.APPLYING
        )

    def _current_drafts(self) -> list[ReplyDraft]:
        return [] if self._run_thread is None else self._run_thread.drafts_snapshot()

    def _update_edit_state(self) -> None:
        draft = next(
            (item for item in self._current_drafts() if item.job_id == self._selected_job_id),
            None,
        )
        editable = draft is not None and draft.status is ReplyJobStatus.AWAITING_REVIEW
        self.save_edit_button.setEnabled(editable)
        self.confirm_button.setEnabled(editable)
        if draft is None:
            self.selected_hint.setText("请选择一条待审核草稿。")

    def _run_status_changed(self, status: str) -> None:
        labels = {
            ReceptionStatus.STARTING.value: "正在启动客服页…",
            ReceptionStatus.RUNNING.value: "运行中",
            ReceptionStatus.STOPPING.value: "正在停止…",
            ReceptionStatus.STOPPED.value: "已停止",
            ReceptionStatus.HALTED.value: "已暂停（需检查配置或页面状态）",
        }
        self._set_status(labels.get(status, status), error=status == ReceptionStatus.HALTED.value)

    def _run_finished(self) -> None:
        self._refresh_timer.stop()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.mode_combo.setEnabled(True)
        self._run_thread = None

    def _approval_finished(self, job_id: str, sent: bool, message: str) -> None:
        """Render the worker's verified send outcome on the UI thread."""
        self.refresh_drafts()
        if sent and self._selected_job_id == job_id:
            self._selected_job_id = None
            self.draft_editor.clear()
            self._update_edit_state()
        self._set_status(message, error=not sent)

    def _price_change_finished(self, task_id: str, applied: bool, message: str) -> None:
        """Show the verified price-change outcome on the UI thread."""
        del task_id
        self.refresh_price_changes()
        self._set_status(message, error=not applied)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("tone", "error" if error else "success")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_changed.emit(message)


def _deepseek_settings(repository: CustomerServiceRepository):
    from xianyu_assistant.customer_service.models import DeepSeekSettings

    return DeepSeekSettings(
        base_url=repository.get_setting("deepseek_base_url", "https://api.deepseek.com")
        or "https://api.deepseek.com",
        text_model=repository.get_setting("deepseek_text_model", "deepseek-chat")
        or "deepseek-chat",
        vision_model=repository.get_setting("deepseek_vision_model", "deepseek-chat")
        or "deepseek-chat",
    )


def _conversation_label(conversation_key: str) -> str:
    return conversation_key if len(conversation_key) <= 22 else f"…{conversation_key[-20:]}"


def _compact_conversation_label(conversation_key: str) -> str:
    """Keep opaque platform session IDs readable in the narrow notification pane."""
    if conversation_key.startswith(("session-", "dom-")):
        return f"会话 …{conversation_key[-8:]}"
    return _conversation_label(conversation_key)


def _status_label(status: ReplyJobStatus) -> str:
    return {
        ReplyJobStatus.DEBOUNCING: "等待合并消息",
        ReplyJobStatus.AWAITING_REVIEW: "待人工审核",
        ReplyJobStatus.SENT: "已发送",
        ReplyJobStatus.HANDOFF: "转人工",
        ReplyJobStatus.SUPERSEDED: "已被新消息替代",
        ReplyJobStatus.FAILED: "失败",
    }.get(status, status.value)


def _price_change_status_label(status: PriceChangeStatus) -> str:
    return {
        PriceChangeStatus.AWAITING_REVIEW: "待人工确认",
        PriceChangeStatus.APPLYING: "正在修改",
        PriceChangeStatus.APPLIED: "已修改",
        PriceChangeStatus.NEEDS_CONFIGURATION: "需补充价格配置",
        PriceChangeStatus.SUPERSEDED: "已被新消息替代",
        PriceChangeStatus.FAILED: "需人工核对",
    }[status]


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, CustomerServiceWorkerError):
        return str(error)
    return "客服接待启动或轮询失败，已安全暂停。"

"""Checks for the Qt customer-service run boundary."""

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHeaderView

import xianyu_assistant.ui.customer_service_panel as panel_module
from xianyu_assistant.browser.manager import BrowserConnectionConfig
from xianyu_assistant.customer_service.models import (
    PriceChangeDraft,
    PriceChangeStatus,
    ReceptionMode,
    ReceptionStatus,
    ReplyDraft,
    ReplyJobStatus,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.ui.customer_service_panel import (
    CustomerServicePanel,
    CustomerServiceRunThread,
)


class FakeBrowserManager:
    def __init__(self, _config: BrowserConnectionConfig) -> None:
        self.connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True


class FakeCustomerServiceWorker:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.status = ReceptionStatus.RUNNING
        self.stopped = False

    def start(self) -> ReceptionStatus:
        return self.status

    def stop(self, _reason: str) -> ReceptionStatus:
        self.stopped = True
        self.status = ReceptionStatus.STOPPED
        return self.status

    def request_stop(self, _reason: str) -> None:
        self.status = ReceptionStatus.STOPPED

    def run_once(self) -> int:
        return 0

    @property
    def drafts(self) -> object:
        raise AssertionError("the run-boundary test does not read drafts")


def test_run_thread_connects_browser_before_starting_worker(
    qapp: object,
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    managers: list[FakeBrowserManager] = []

    def make_manager(config: BrowserConnectionConfig) -> FakeBrowserManager:
        manager = FakeBrowserManager(config)
        managers.append(manager)
        return manager

    monkeypatch.setattr(panel_module, "BrowserManager", make_manager)  # type: ignore[attr-defined]
    monkeypatch.setattr(panel_module, "CustomerServiceWorker", FakeCustomerServiceWorker)  # type: ignore[attr-defined]

    thread = CustomerServiceRunThread(repository, BrowserConnectionConfig(port=9333))
    thread._stop_requested.set()  # type: ignore[attr-defined]
    thread.run()

    assert len(managers) == 1
    assert managers[0].connected is True
    assert managers[0].disconnected is True


class FakeApprovalThread:
    def __init__(self, draft: ReplyDraft) -> None:
        self.draft = draft
        self.approved: tuple[str, str] | None = None

    def drafts_snapshot(self) -> list[ReplyDraft]:
        return [self.draft]

    def approve_draft(self, job_id: str, text: str) -> None:
        self.approved = (job_id, text)


class FakeSignal:
    def connect(self, _callback: object) -> None:
        pass


class FakeStartThread:
    instances: ClassVar[list["FakeStartThread"]] = []

    def __init__(self, *_args: object, mode: ReceptionMode, **_kwargs: object) -> None:
        self.mode = mode
        self.started = False
        self.status_changed = FakeSignal()
        self.drafts_changed = FakeSignal()
        self.run_error = FakeSignal()
        self.approval_finished = FakeSignal()
        self.finished = FakeSignal()
        self.instances.append(self)

    def isRunning(self) -> bool:
        return self.started

    def start(self) -> None:
        self.started = True


def test_panel_enables_explicit_confirm_and_passes_visible_edit(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServicePanel(
        repository,
        lambda: BrowserConnectionConfig(port=9333),
    )
    draft = ReplyDraft(
        job_id="job-1",
        conversation_key="conversation-1",
        batch_fingerprint="batch-1",
        reply_text="原草稿",
        status=ReplyJobStatus.AWAITING_REVIEW,
    )
    thread = FakeApprovalThread(draft)
    panel._run_thread = thread  # type: ignore[assignment]
    panel._selected_job_id = draft.job_id
    panel.draft_editor.setPlainText("人工修改后的回复")

    panel._update_edit_state()
    assert panel.confirm_button.isEnabled() is True

    panel.confirm_selected_draft()

    assert thread.approved == ("job-1", "人工修改后的回复")
    assert panel.confirm_button.isEnabled() is False


def test_auto_send_cancel_keeps_reception_stopped(
    qapp: object,
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))
    panel.mode_combo.setCurrentIndex(1)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        panel_module.QMessageBox,
        "warning",
        lambda *_args, **_kwargs: panel_module.QMessageBox.StandardButton.Cancel,
    )

    panel.start_reception()

    assert panel._run_thread is None
    assert "已取消全自动客服" in panel.status_label.text()
    assert panel.start_button.isEnabled() is True


def test_auto_send_confirm_starts_thread_and_locks_mode(
    qapp: object,
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    FakeStartThread.instances.clear()
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))
    panel.mode_combo.setCurrentIndex(1)
    monkeypatch.setattr(panel_module, "CustomerServiceRunThread", FakeStartThread)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        panel_module.QMessageBox,
        "warning",
        lambda *_args, **_kwargs: panel_module.QMessageBox.StandardButton.Yes,
    )

    panel.start_reception()

    assert len(FakeStartThread.instances) == 1
    assert FakeStartThread.instances[0].mode is ReceptionMode.AUTO_SEND
    assert FakeStartThread.instances[0].started is True
    assert panel.mode_combo.isEnabled() is False
    assert "全自动客服" in panel.status_label.text()
    panel._refresh_timer.stop()


def test_panel_lists_and_resolves_non_blocking_handoff_notification(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_handoff_event(
        conversation_key="conversation-1",
        reason="涉及售后争议",
        created_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
    )
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))

    assert panel.handoff_table.rowCount() == 1
    assert "待处理 1 条" in panel.handoff_hint.text()
    panel.handoff_table.selectRow(0)
    assert panel.resolve_handoff_button.isEnabled() is True

    panel.resolve_selected_handoff()

    assert panel.handoff_table.rowCount() == 0
    assert repository.has_open_handoff("conversation-1") is False


def test_customer_service_workspace_uses_balanced_columns_and_hides_internal_ids(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))

    assert panel.workspace_splitter.orientation() is Qt.Orientation.Horizontal
    assert panel.draft_table.isColumnHidden(3) is True
    assert panel.draft_table.verticalHeader().isVisible() is False
    assert panel.handoff_table.verticalHeader().isVisible() is False
    assert (
        panel.draft_table.horizontalHeader().sectionResizeMode(2)
        is QHeaderView.ResizeMode.Stretch
    )
    assert (
        panel.handoff_table.horizontalHeader().sectionResizeMode(1)
        is QHeaderView.ResizeMode.Stretch
    )


def test_price_change_filter_defaults_to_attention_without_deleting_history(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    for task_id, status in (
        ("pending", PriceChangeStatus.AWAITING_REVIEW),
        ("failed", PriceChangeStatus.FAILED),
        ("applied", PriceChangeStatus.APPLIED),
    ):
        repository.save_price_change_draft(
            PriceChangeDraft(
                task_id=task_id,
                conversation_key=f"conversation-{task_id}",
                customer_message_key=f"message-{task_id}",
                product_key="battery-6020",
                product_name="60V20Ah 原装铁塔",
                customer_offer="390.00",
                proposed_price="398.00",
                minimum_price="380.00",
                listed_price="398.00",
                status=status,
                failure_reason="页面确认失败" if status is PriceChangeStatus.FAILED else None,
            )
        )

    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))

    assert panel.price_filter_combo.currentData() == "attention"
    assert panel.price_change_table.rowCount() == 2
    assert "2 需处理" in panel.price_change_group.title()

    panel.price_filter_combo.setCurrentIndex(1)
    assert panel.price_change_table.rowCount() == 3
    panel.price_filter_combo.setCurrentIndex(2)
    assert panel.price_change_table.rowCount() == 1


def test_price_change_selection_and_filter_do_not_resize_workspace(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    for task_id, rationale in (
        ("short", "按顾客报价生成"),
        ("long", "根据当前对话确认的型号和顾客报价生成。" * 30),
    ):
        repository.save_price_change_draft(
            PriceChangeDraft(
                task_id=task_id,
                conversation_key=f"conversation-{task_id}",
                customer_message_key=f"message-{task_id}",
                product_key="battery-6030",
                product_name="60V30Ah 原装铁塔",
                customer_offer="650.00",
                proposed_price="650.00",
                minimum_price="630.00",
                listed_price="678.00",
                rationale=rationale,
            )
        )
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))
    panel.resize(1160, 700)
    panel.show()
    qapp.processEvents()  # type: ignore[attr-defined]
    initial_group_height = panel.price_change_group.height()
    initial_splitter_sizes = panel.workspace_splitter.sizes()

    panel.price_change_table.selectRow(0)
    qapp.processEvents()  # type: ignore[attr-defined]
    panel.price_change_table.selectRow(1)
    qapp.processEvents()  # type: ignore[attr-defined]

    assert panel.price_change_group.height() == initial_group_height
    assert panel.workspace_splitter.sizes() == initial_splitter_sizes
    assert panel.price_change_detail.height() == 36

    panel.price_filter_combo.setCurrentIndex(2)
    qapp.processEvents()  # type: ignore[attr-defined]
    assert panel.price_change_group.height() == initial_group_height
    assert panel.workspace_splitter.sizes() == initial_splitter_sizes


def test_failed_price_change_can_be_deleted_after_confirmation(
    qapp: object,
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_price_change_draft(
        PriceChangeDraft(
            task_id="failed-price-change",
            conversation_key="conversation-1",
            customer_message_key="message-1",
            product_key="battery-6030",
            product_name="60V30Ah 原装铁塔",
            customer_offer="650.00",
            proposed_price="650.00",
            minimum_price="650.00",
            listed_price="678.00",
            status=PriceChangeStatus.FAILED,
            failure_reason="页面改价结果无法确认",
        )
    )
    panel = CustomerServicePanel(repository, lambda: BrowserConnectionConfig(port=9333))
    panel.price_change_table.selectRow(0)
    assert panel.confirm_price_change_button.isEnabled() is False
    assert panel.delete_price_change_button.isEnabled() is True
    monkeypatch.setattr(  # type: ignore[attr-defined]
        panel_module.QMessageBox,
        "warning",
        lambda *_args, **_kwargs: panel_module.QMessageBox.StandardButton.Yes,
    )

    panel.delete_selected_price_change()

    assert repository.list_price_change_drafts() == []
    assert panel.price_change_table.rowCount() == 0
    assert panel.delete_price_change_button.isEnabled() is False
    assert "本地改价记录已删除" in panel.status_label.text()

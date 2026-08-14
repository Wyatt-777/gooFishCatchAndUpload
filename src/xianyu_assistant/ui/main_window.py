"""Main window for the guided seller-homepage import workflow."""

import os
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from xianyu_assistant.browser.connection_worker import BrowserConnectionWorker
from xianyu_assistant.browser.launcher import BrowserLauncher, BrowserLaunchError
from xianyu_assistant.browser.manager import BrowserConnectionConfig
from xianyu_assistant.crawler.xianyu_crawler import (
    SellerProfileUrlError,
    validate_seller_profile_url,
)
from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.domain.models import TaskStatus
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository
from xianyu_assistant.services.collection_worker import CollectionWorker
from xianyu_assistant.services.image_download_worker import ImageDownloadWorker
from xianyu_assistant.services.publish_worker import PublishWorker
from xianyu_assistant.ui.browser_connection_panel import BrowserConnectionPanel
from xianyu_assistant.ui.product_table import ProductTable
from xianyu_assistant.ui.publish_dialog import PublishDialog
from xianyu_assistant.ui.seller_source_panel import SellerSourcePanel


class MainWindow(QMainWindow):
    """Show the normal workflow on one screen and hide advanced controls."""

    def __init__(
        self,
        repository: SqliteRepository | None = None,
        *,
        auto_start_browser: bool = True,
    ) -> None:
        super().__init__()
        self.setWindowTitle("闲鱼铺货助手")
        self.resize(1160, 760)

        self.repository = repository or SqliteRepository(
            _default_database_path(),
            debug_writer=DebugArtifactWriter(),
        )
        self.repository.initialize()
        self.current_collection_id: int | None = None
        self.collection_worker: CollectionWorker | None = None
        self.image_worker: ImageDownloadWorker | None = None
        self.publish_worker: PublishWorker | None = None
        self.publish_dialog: PublishDialog | None = None

        self.seller_source_panel = SellerSourcePanel()
        self.product_table = ProductTable()
        self.browser_connection_panel = BrowserConnectionPanel()
        self.browser_worker: BrowserConnectionWorker | None = None
        self.workflow_page = self._build_workflow_page()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.workflow_page, "开始处理")
        self.tabs.addTab(self._build_settings_page(), "浏览器设置")
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("请按页面上方的步骤完成导入。")

        self.seller_source_panel.browser_requested.connect(self.start_browser_and_connect)
        self.seller_source_panel.collect_requested.connect(self.collect_seller_profile)
        self.seller_source_panel.download_requested.connect(self.download_images)
        self.seller_source_panel.settings_requested.connect(self.show_settings)
        self.product_table.publish_requested.connect(self.show_publish_dialog)
        self.browser_connection_panel.connect_requested.connect(self.connect_to_browser)
        self.browser_connection_panel.launch_requested.connect(self.start_browser_and_connect)
        if auto_start_browser:
            QTimer.singleShot(0, self.start_browser_and_connect)

    def _build_workflow_page(self) -> QWidget:
        """Keep all daily actions together so the user need not navigate tabs."""

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self.seller_source_panel)
        layout.addWidget(self.product_table, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.browser_connection_panel)

        data_group = QGroupBox("本地数据")
        data_form = QFormLayout(data_group)
        data_form.addRow("商品会话：", QLabel("SQLite 本地缓存（不显示任务列表）"))
        data_form.addRow("图片：", QLabel("写入 output/images/ 目录"))
        data_form.addRow("调试证据：", QLabel("默认写入 debug/ 目录"))
        layout.addWidget(data_group)
        layout.addStretch()
        return page

    def show_settings(self) -> None:
        self.tabs.setCurrentIndex(1)

    def start_browser_and_connect(self, config: BrowserConnectionConfig | None = None) -> None:
        config = config or self.browser_connection_panel.connection_config()
        self.browser_connection_panel.set_launching()
        self.seller_source_panel.set_browser_connecting()
        try:
            result = BrowserLauncher(config).start()
        except BrowserLaunchError as error:
            self.browser_connection_panel.set_connection_error(str(error))
            self.browser_connection_panel.set_idle()
            self.seller_source_panel.set_browser_error(str(error))
            self.statusBar().showMessage(str(error), 10_000)
            return

        if result.reused_running_browser:
            self.statusBar().showMessage("已复用本机浏览器，正在连接闲鱼…")
        else:
            assert result.executable is not None
            self.statusBar().showMessage(f"已启动 {result.executable.name}，正在连接闲鱼…")
        self.connect_to_browser(config, attempts=12)

    def connect_to_browser(self, config: BrowserConnectionConfig, *, attempts: int = 1) -> None:
        if self.browser_worker is not None and self.browser_worker.isRunning():
            return
        self.browser_connection_panel.set_connecting()
        self.seller_source_panel.set_browser_connecting()
        self.statusBar().showMessage(f"正在连接 {config.endpoint_url} …")

        worker = BrowserConnectionWorker(config, attempts=attempts)
        worker.connection_succeeded.connect(self._browser_connection_succeeded)
        worker.connection_failed.connect(self._browser_connection_failed)
        worker.finished.connect(self.browser_connection_panel.set_idle)
        self.browser_worker = worker
        worker.start()

    def _browser_connection_succeeded(self, page_url: str) -> None:
        self.browser_connection_panel.set_connected(page_url)
        self.seller_source_panel.set_browser_ready()
        self.statusBar().showMessage("浏览器已连接，可以开始导入卖家商品。", 5000)

    def _browser_connection_failed(self, message: str) -> None:
        self.browser_connection_panel.set_connection_error(message)
        self.seller_source_panel.set_browser_error(message)
        self.statusBar().showMessage("浏览器连接失败，可点“浏览器设置”重试。", 7000)

    def collect_seller_profile(self, source_url: str, max_products: int) -> None:
        """Start one bounded seller homepage import after local URL validation."""

        if self.collection_worker is not None and self.collection_worker.isRunning():
            self.statusBar().showMessage("卖家主页采集正在进行中。", 3000)
            return
        try:
            profile_url = validate_seller_profile_url(source_url)
        except SellerProfileUrlError as error:
            self.seller_source_panel.set_error(str(error))
            self.statusBar().showMessage(str(error), 7000)
            return

        # This history is a recoverable cache. It is deliberately not a user-facing task list.
        collection = self.repository.create_task(profile_url)
        self.repository.transition_task(collection.id, TaskStatus.RUNNING)
        worker = CollectionWorker(
            collection.id,
            profile_url,
            max_products,
            self.repository,
            self.browser_connection_panel.connection_config(),
        )
        worker.collection_completed.connect(self._collection_completed)
        worker.collection_failed.connect(self._collection_failed)
        worker.finished.connect(self._collection_worker_finished)
        self.collection_worker = worker
        self.seller_source_panel.set_collecting()
        self.statusBar().showMessage("正在读取卖家主页并补全商品详情…")
        worker.start()

    def _collection_completed(self, collection_id: int, inserted_count: int) -> None:
        self.current_collection_id = collection_id
        products = self.repository.list_products(collection_id)
        self.product_table.set_products("卖家主页导入", products)
        self.tabs.setCurrentWidget(self.workflow_page)
        self.seller_source_panel.set_result(len(products))
        self.statusBar().showMessage(f"采集完成：新增 {inserted_count} 件商品。", 5000)

    def _collection_failed(self, _collection_id: int, message: str) -> None:
        self.seller_source_panel.set_error(message)
        self.statusBar().showMessage(f"卖家主页采集失败：{message}", 10_000)

    def _collection_worker_finished(self) -> None:
        self.seller_source_panel.set_idle()

    def download_images(self) -> None:
        """Download images belonging to the one visible import session."""

        if self.current_collection_id is None:
            self.statusBar().showMessage("请先完成第 2 步：采集卖家商品。", 3000)
            return
        if self.image_worker is not None and self.image_worker.isRunning():
            self.statusBar().showMessage("图片下载正在进行中。", 3000)
            return
        worker = ImageDownloadWorker(self.current_collection_id, self.repository, _output_directory())
        worker.completed.connect(self._images_downloaded)
        self.image_worker = worker
        self.seller_source_panel.set_downloading()
        worker.start()
        self.statusBar().showMessage("正在下载当前批次的全部商品图片…")

    def _images_downloaded(self, collection_id: int, count: int, errors: str) -> None:
        products = self.repository.list_products(collection_id)
        self.product_table.set_products("卖家主页导入", products)
        self.seller_source_panel.set_download_result(count, bool(errors))
        if errors:
            self.statusBar().showMessage(f"已下载 {count} 张图片；部分失败：{errors}", 8000)
        else:
            self.statusBar().showMessage(f"图片下载完成：{count} 张。", 5000)

    def show_publish_dialog(self, product: object) -> None:
        if self.publish_dialog is not None:
            self.publish_dialog.raise_()
            self.publish_dialog.activateWindow()
            return
        dialog = PublishDialog(product)  # type: ignore[arg-type]
        dialog.prefill_requested.connect(self.prepare_publish)
        dialog.finished.connect(self._publish_dialog_finished)
        self.publish_dialog = dialog
        dialog.show()

    def prepare_publish(self, draft: object) -> None:
        if self.publish_worker is not None and self.publish_worker.isRunning():
            self.statusBar().showMessage("发布页预填正在进行中。", 3000)
            return
        worker = PublishWorker(
            draft,  # type: ignore[arg-type]
            self.browser_connection_panel.connection_config(),
        )
        worker.prepared.connect(self._publish_prepared)
        worker.failed.connect(self._publish_failed)
        self.publish_worker = worker
        worker.start()
        self.statusBar().showMessage("正在预填发布页；程序不会点击最终发布按钮。")

    def _publish_prepared(
        self,
        _page_url: str,
        category: str,
        image_count: int,
        filled_attribute_count: int,
        pending_attribute_count: int,
    ) -> None:
        if self.publish_dialog is not None:
            self.publish_dialog.accept()
        category_note = category or "请在发布页手动选择分类"
        attribute_note = f"已填 {filled_attribute_count} 项属性"
        if pending_attribute_count:
            attribute_note += f"，另有 {pending_attribute_count} 项需手动选择"
        self.statusBar().showMessage(
            f"已预填 {image_count} 张图片，{attribute_note}，分类：{category_note}；请在浏览器中检查后手动发布。",
            10_000,
        )

    def _publish_failed(self, message: str) -> None:
        if self.publish_dialog is not None:
            self.publish_dialog.set_prefill_failure(message)
        self.statusBar().showMessage(f"发布页未预填：{message}", 10_000)
        QMessageBox.warning(self, "发布页未预填", message)

    def _publish_dialog_finished(self, _result: int) -> None:
        self.publish_dialog = None


def _default_database_path() -> Path:
    app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "XianyuAssistant"
    return app_data / "xianyu_assistant.db"


def _output_directory() -> Path:
    return Path.cwd() / "output"

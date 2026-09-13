"""Local configuration UI for the CS-2 customer-service foundation."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
from xianyu_assistant.customer_service.current_catalog import install_current_catalog
from xianyu_assistant.customer_service.deepseek_client import (
    DeepSeekClient,
    DeepSeekClientError,
)
from xianyu_assistant.customer_service.history_sync import (
    CustomerServiceHistorySynchronizer,
    LiveSyncResult,
)
from xianyu_assistant.customer_service.importer import ChatHistoryImporter, ChatImportError
from xianyu_assistant.customer_service.knowledge_importer import (
    CleanedKnowledgeImporter,
    CleanedKnowledgeImportError,
)
from xianyu_assistant.customer_service.media_assets import MediaAssetError, register_media_asset
from xianyu_assistant.customer_service.models import DeepSeekSettings, ProductKnowledge
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import (
    CredentialStore,
    CredentialStoreUnavailableError,
    KeyringCredentialStore,
)


class LiveChatSyncThread(QThread):
    """Run read-only live chat synchronization outside the Qt event loop."""

    progress = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        repository: CustomerServiceRepository,
        browser_config_provider: Callable[[], BrowserConnectionConfig],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._browser_config_provider = browser_config_provider

    def run(self) -> None:
        manager = BrowserManager(self._browser_config_provider())
        try:
            manager.connect()
            adapter = PlaywrightXianyuChatAdapter(manager, REAL_XIANYU_CHAT_CONTRACT)
            adapter.open_dedicated_chat_page()
            result = CustomerServiceHistorySynchronizer(
                self._repository,
                progress=self.progress.emit,
            ).sync(adapter)
            self.completed.emit(result)
        except Exception as error:  # noqa: BLE001 - show bounded failure in the settings page
            self.failed.emit(str(error).strip()[:240] or error.__class__.__name__)
        finally:
            manager.disconnect()


class CustomerServiceSettingsPanel(QWidget):
    """Expose only local configuration and explicit test/import actions."""

    status_changed = pyqtSignal(str)

    def __init__(
        self,
        repository: CustomerServiceRepository,
        *,
        credential_store: CredentialStore | None = None,
        browser_config_provider: Callable[[], BrowserConnectionConfig] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.credential_store = credential_store or KeyringCredentialStore()
        self._browser_config_provider = browser_config_provider or self._stored_browser_config
        self._sync_thread: LiveChatSyncThread | None = None
        self.importer = ChatHistoryImporter()
        self.knowledge_importer = CleanedKnowledgeImporter()
        self._selected_import_path: Path | None = None
        self._selected_knowledge_path: Path | None = None
        self.setObjectName("customerServiceSettings")

        self.base_url_input = QLineEdit(repository.get_setting("deepseek_base_url", "https://api.deepseek.com"))
        self.text_model_input = QLineEdit(repository.get_setting("deepseek_text_model", "deepseek-chat"))
        self.vision_model_input = QLineEdit(repository.get_setting("deepseek_vision_model", "deepseek-chat"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("已保存的 Key 不会回显；留空表示保持不变")
        self.save_config_button = QPushButton("保存配置")
        self.test_connection_button = QPushButton("测试连接")
        self.clear_key_button = QPushButton("清除 Key")
        self.credential_status_label = QLabel(self._credential_status())
        self.config_status_label = QLabel("仅保存 Base URL 和模型名；API Key 存入系统凭据存储。")
        self.config_status_label.setObjectName("settingsStatus")
        self.config_status_label.setProperty("tone", "neutral")
        self.config_status_label.setWordWrap(True)

        api_group = QGroupBox("DeepSeek 配置")
        api_form = QFormLayout(api_group)
        api_form.addRow("Base URL：", self.base_url_input)
        api_form.addRow("文本模型：", self.text_model_input)
        api_form.addRow("视觉模型：", self.vision_model_input)
        api_form.addRow("API Key：", self.api_key_input)
        api_form.addRow("凭据状态：", self.credential_status_label)
        api_buttons = QHBoxLayout()
        api_buttons.addWidget(self.save_config_button)
        api_buttons.addWidget(self.test_connection_button)
        api_buttons.addWidget(self.clear_key_button)
        api_buttons.addStretch()
        api_form.addRow(api_buttons)
        api_form.addRow(self.config_status_label)

        self.product_table = QTableWidget(0, 6)
        self.product_table.setHorizontalHeaderLabels(("商品 Key", "名称", "平台 ID", "标价", "最低价", "状态"))
        self._configure_table(self.product_table)
        self.sync_chat_button = QPushButton("从当前账号抓取并整理")
        self.sync_chat_button.setObjectName("primaryAction")
        self.add_product_button = QPushButton("手动新增（高级）")
        self.edit_product_button = QPushButton("编辑商品")
        self.delete_product_button = QPushButton("删除商品")
        product_buttons = QHBoxLayout()
        product_buttons.addWidget(self.sync_chat_button)
        product_buttons.addWidget(self.add_product_button)
        product_buttons.addWidget(self.edit_product_button)
        product_buttons.addWidget(self.delete_product_button)
        product_buttons.addStretch()
        product_group = QGroupBox("商品知识与最低价")
        product_layout = QVBoxLayout(product_group)
        product_hint = QLabel(
            "优先使用当前登录账号自动整理：商品信息按商品归并，聊天正文仅保存在本地并先做隐私脱敏。"
        )
        product_hint.setWordWrap(True)
        product_hint.setObjectName("mutedText")
        product_layout.addWidget(product_hint)
        product_layout.addWidget(self.product_table)
        product_layout.addLayout(product_buttons)

        self.media_table = QTableWidget(0, 6)
        self.media_table.setHorizontalHeaderLabels(("资源 ID", "名称", "商品 Key", "场景", "类型", "哈希"))
        self._configure_table(self.media_table)
        self.add_media_button = QPushButton("登记图片")
        self.delete_media_button = QPushButton("删除图片")
        media_buttons = QHBoxLayout()
        media_buttons.addWidget(self.add_media_button)
        media_buttons.addWidget(self.delete_media_button)
        media_buttons.addStretch()
        media_group = QGroupBox("已登记本地图片（本阶段只登记，不发送）")
        media_layout = QVBoxLayout(media_group)
        media_layout.addWidget(self.media_table)
        media_layout.addLayout(media_buttons)

        self.import_path_label = QLabel("尚未选择聊天记录文件。")
        self.import_path_label.setWordWrap(True)
        self.import_preview_label = QLabel("导入预览会显示会话、消息、问答轮次和错误数，不显示聊天正文。")
        self.import_preview_label.setWordWrap(True)
        self.choose_import_button = QPushButton("选择 ZIP / JSON / CSV")
        self.import_button = QPushButton("导入聊天记录")
        self.import_button.setEnabled(False)
        import_buttons = QHBoxLayout()
        import_buttons.addWidget(self.choose_import_button)
        import_buttons.addWidget(self.import_button)
        import_buttons.addStretch()
        import_group = QGroupBox("历史聊天导入")
        import_layout = QVBoxLayout(import_group)
        import_layout.addWidget(self.import_path_label)
        import_layout.addWidget(self.import_preview_label)
        import_layout.addLayout(import_buttons)

        self.knowledge_path_label = QLabel("尚未选择清洗知识库文件。")
        self.knowledge_path_label.setWordWrap(True)
        self.knowledge_preview_label = QLabel(
            "导入前仅显示商品、知识文档、历史问答和错误数量，不显示聊天正文。"
        )
        self.knowledge_preview_label.setWordWrap(True)
        self.choose_knowledge_button = QPushButton("选择清洗知识库 JSON")
        self.import_knowledge_button = QPushButton("导入清洗知识库")
        self.import_knowledge_button.setEnabled(False)
        knowledge_buttons = QHBoxLayout()
        knowledge_buttons.addWidget(self.choose_knowledge_button)
        knowledge_buttons.addWidget(self.import_knowledge_button)
        knowledge_buttons.addStretch()
        knowledge_group = QGroupBox("清洗知识库导入")
        knowledge_layout = QVBoxLayout(knowledge_group)
        knowledge_layout.addWidget(self.knowledge_path_label)
        knowledge_layout.addWidget(self.knowledge_preview_label)
        knowledge_layout.addLayout(knowledge_buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("客服设置")
        title.setObjectName("pageTitle")
        maintenance_hint = QLabel(f"本地数据目录：{self.repository.database_path.parent}")
        maintenance_hint.setWordWrap(True)
        self.open_data_directory_button = QPushButton("打开数据目录")
        self.cleanup_audit_button = QPushButton("立即清理过期审计记录")
        maintenance_buttons = QHBoxLayout()
        maintenance_buttons.addWidget(self.open_data_directory_button)
        maintenance_buttons.addWidget(self.cleanup_audit_button)
        maintenance_buttons.addStretch()
        maintenance_group = QGroupBox("本地数据与清理")
        maintenance_layout = QVBoxLayout(maintenance_group)
        maintenance_layout.addWidget(maintenance_hint)
        maintenance_layout.addLayout(maintenance_buttons)

        subtitle = QLabel("客服接待保持人工审核模式；本页管理本地知识、凭据、导入与清理。")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(api_group)
        layout.addWidget(product_group)
        layout.addWidget(media_group)
        layout.addWidget(import_group)
        layout.addWidget(knowledge_group)
        layout.addWidget(maintenance_group)

        self.save_config_button.clicked.connect(self.save_configuration)
        self.test_connection_button.clicked.connect(self.test_connection)
        self.clear_key_button.clicked.connect(self.clear_api_key)
        self.add_product_button.clicked.connect(self.add_product)
        self.sync_chat_button.clicked.connect(self.sync_live_chats)
        self.edit_product_button.clicked.connect(self.edit_product)
        self.delete_product_button.clicked.connect(self.delete_product)
        self.add_media_button.clicked.connect(self.add_media)
        self.delete_media_button.clicked.connect(self.delete_media)
        self.choose_import_button.clicked.connect(self.choose_import)
        self.import_button.clicked.connect(self.import_history)
        self.choose_knowledge_button.clicked.connect(self.choose_knowledge)
        self.import_knowledge_button.clicked.connect(self.import_cleaned_knowledge)
        self.open_data_directory_button.clicked.connect(self.open_data_directory)
        self.cleanup_audit_button.clicked.connect(self.cleanup_expired_audit)
        self._refresh_products()
        self._refresh_media_assets()

    def save_configuration(self) -> None:
        """Save non-secret settings and optionally replace the secure API key."""
        try:
            settings = self._settings_from_inputs()
            api_key = self.api_key_input.text().strip()
            if api_key:
                self.credential_store.set("deepseek_api_key", api_key)
                self.api_key_input.clear()
            self.repository.set_setting("deepseek_base_url", settings.base_url)
            self.repository.set_setting("deepseek_text_model", settings.text_model)
            self.repository.set_setting("deepseek_vision_model", settings.vision_model)
        except (ValueError, CredentialStoreUnavailableError) as error:
            self._set_status(f"配置未保存：{error}", error=True)
            return
        self.credential_status_label.setText(self._credential_status())
        self._set_status("客服配置已保存。")

    def test_connection(self) -> None:
        """Send only the fixed connection probe; never send UI or chat content."""
        try:
            client = DeepSeekClient(self._settings_from_inputs(), self.credential_store)
            response = client.test_connection()
        except (ValueError, DeepSeekClientError) as error:
            self._set_status(f"连接测试失败：{error}", error=True)
            return
        self._set_status(f"连接测试成功（模型：{response.model or self.text_model_input.text().strip()}）。")

    def clear_api_key(self) -> None:
        try:
            self.credential_store.delete("deepseek_api_key")
        except CredentialStoreUnavailableError as error:
            self._set_status(f"清除失败：{error}", error=True)
            return
        self.api_key_input.clear()
        self.credential_status_label.setText(self._credential_status())
        self._set_status("DeepSeek API Key 已从安全凭据存储清除。")

    def add_product(self) -> None:
        dialog = ProductKnowledgeDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.save_product_knowledge(dialog.product())
            self._refresh_products()

    def sync_live_chats(self) -> None:
        """Start a read-only scrape of every conversation on the logged-in account."""
        if self._sync_thread is not None and self._sync_thread.isRunning():
            self._set_status("聊天记录整理正在进行中，请稍候。")
            return
        thread = LiveChatSyncThread(self.repository, self._browser_config_provider, parent=self)
        thread.progress.connect(self._set_status)
        thread.completed.connect(self._live_sync_completed)
        thread.failed.connect(self._live_sync_failed)
        thread.finished.connect(self._live_sync_finished)
        self._sync_thread = thread
        self.sync_chat_button.setEnabled(False)
        self._set_status("正在连接当前浏览器并读取全部闲鱼会话…")
        thread.start()

    def _live_sync_completed(self, result: LiveSyncResult) -> None:
        install_current_catalog(self.repository)
        self._refresh_products()
        message = (
            f"整理完成：{result.conversation_count} 个会话，{result.message_count} 条消息，"
            f"{result.product_count} 个商品，{result.example_count} 个历史问答。"
        )
        if result.errors:
            message += f" 另有 {len(result.errors)} 个会话未完成。"
        self._set_status(message, error=bool(result.errors))

    def _live_sync_failed(self, message: str) -> None:
        self._set_status(f"聊天记录整理失败：{message}", error=True)

    def _live_sync_finished(self) -> None:
        self.sync_chat_button.setEnabled(True)
        self._sync_thread = None

    def _stored_browser_config(self) -> BrowserConnectionConfig:
        raw_port = self.repository.get_setting("browser_cdp_port", "9333") or "9333"
        try:
            port = int(raw_port)
        except ValueError:
            port = 9333
        return BrowserConnectionConfig(port=port)

    def edit_product(self) -> None:
        product = self._selected_product()
        if product is None:
            self._set_status("请先选择一个商品。", error=True)
            return
        dialog = ProductKnowledgeDialog(product, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.save_product_knowledge(dialog.product())
            self._refresh_products()

    def delete_product(self) -> None:
        product = self._selected_product()
        if product is None:
            self._set_status("请先选择一个商品。", error=True)
            return
        answer = QMessageBox.question(self, "删除商品知识", f"确认删除“{product.name}”及其别名吗？")
        if answer == QMessageBox.StandardButton.Yes:
            self.repository.delete_product_knowledge(product.product_key)
            self._refresh_products()

    def add_media(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "选择已登记图片",
            "",
            "图片 (*.jpg *.jpeg *.png *.webp *.gif)",
        )
        if not file_name:
            return
        dialog = MediaAssetDialog(Path(file_name), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            asset = register_media_asset(
                Path(file_name),
                asset_id=uuid.uuid4().hex,
                product_key=dialog.product_key_input.text().strip() or None,
                display_name=dialog.display_name_input.text().strip(),
                scene_tag=dialog.scene_tag_input.text().strip(),
                description=dialog.description_input.toPlainText().strip(),
            )
            self.repository.save_media_asset(asset)
        except MediaAssetError as error:
            self._set_status(f"图片登记失败：{error}", error=True)
            return
        self._refresh_media_assets()

    def delete_media(self) -> None:
        row = self.media_table.currentRow()
        if row < 0:
            self._set_status("请先选择一个图片资源。", error=True)
            return
        asset_id = self.media_table.item(row, 0).text()
        self.repository.delete_media_asset(asset_id)
        self._refresh_media_assets()

    def choose_import(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "选择聊天记录",
            "",
            "聊天记录 (*.zip *.json *.csv)",
        )
        if not file_name:
            return
        self._selected_import_path = Path(file_name)
        self.import_path_label.setText(str(self._selected_import_path))
        try:
            preview = self.importer.preview(self._selected_import_path)
        except ChatImportError as error:
            self.import_button.setEnabled(False)
            self._set_status(f"聊天记录预览失败：{error}", error=True)
            return
        self.import_preview_label.setText(
            f"{preview.conversation_count} 个会话，{preview.message_count} 条消息，"
            f"{preview.example_count} 个可用问答轮次，错误 {preview.error_count} 条。"
        )
        self.import_button.setEnabled(True)
        self._set_status("聊天记录预览完成；导入前已执行本地脱敏。")

    def import_history(self) -> None:
        if self._selected_import_path is None:
            self._set_status("请先选择聊天记录文件。", error=True)
            return
        try:
            bundle = self.importer.parse(self._selected_import_path)
            inserted = self.repository.store_import(bundle)
        except ChatImportError as error:
            self._set_status(f"聊天记录导入失败：{error}", error=True)
            return
        self._set_status("聊天记录已导入。" if inserted else "该聊天记录已导入过，未重复写入。")

    def choose_knowledge(self) -> None:
        """Preview a versioned cleaned-knowledge file without changing SQLite."""
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "选择清洗知识库",
            "",
            "清洗知识库 (*.json)",
        )
        if not file_name:
            return
        self._selected_knowledge_path = Path(file_name)
        self.knowledge_path_label.setText(str(self._selected_knowledge_path))
        try:
            preview = self.knowledge_importer.preview(self._selected_knowledge_path)
        except CleanedKnowledgeImportError as error:
            self.import_knowledge_button.setEnabled(False)
            self._set_status(f"清洗知识库预览失败：{error}", error=True)
            return
        self.knowledge_preview_label.setText(
            f"{preview.product_count} 个商品，{preview.chat_document_count} 个商品聊天，"
            f"{preview.example_count} 个历史问答，错误 {preview.error_count} 条。"
        )
        self.import_knowledge_button.setEnabled(True)
        self._set_status("清洗知识库预览完成；导入不会覆盖已有的人工商品事实。")

    def import_cleaned_knowledge(self) -> None:
        """Perform the explicit, atomic knowledge import after preview."""
        if self._selected_knowledge_path is None:
            self._set_status("请先选择清洗知识库文件。", error=True)
            return
        try:
            bundle = self.knowledge_importer.parse(self._selected_knowledge_path)
            inserted = self.repository.store_cleaned_knowledge(bundle)
        except CleanedKnowledgeImportError as error:
            self._set_status(f"清洗知识库导入失败：{error}", error=True)
            return
        install_current_catalog(self.repository)
        self._refresh_products()
        self._set_status(
            "清洗知识库已导入。" if inserted else "该清洗知识库已导入过，未重复写入。"
        )

    def open_data_directory(self) -> None:
        """Open the user-visible directory containing the database and log."""
        directory = self.repository.database_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            self._set_status(f"无法打开数据目录：{directory}", error=True)
            return
        self._set_status(f"已打开本地数据目录：{directory}")

    def cleanup_expired_audit(self) -> None:
        """Delete only audit records older than the configured 15-day retention."""
        try:
            deleted = self.repository.cleanup_expired_audit()
        except OSError as error:
            self._set_status(f"清理失败：{error}", error=True)
            return
        self._set_status(f"过期审计记录清理完成，共删除 {deleted} 条。")

    def _settings_from_inputs(self) -> DeepSeekSettings:
        return DeepSeekSettings(
            base_url=self.base_url_input.text().strip(),
            text_model=self.text_model_input.text().strip(),
            vision_model=self.vision_model_input.text().strip(),
        )

    def _selected_product(self) -> ProductKnowledge | None:
        row = self.product_table.currentRow()
        if row < 0:
            return None
        key = self.product_table.item(row, 0).text()
        return next(
            (
                product
                for product in self.repository.list_product_knowledge(include_disabled=False)
                if product.product_key == key
            ),
            None,
        )

    def _refresh_products(self) -> None:
        self.product_table.setRowCount(0)
        for product in self.repository.list_product_knowledge(include_disabled=False):
            row = self.product_table.rowCount()
            self.product_table.insertRow(row)
            values = (
                product.product_key,
                product.name,
                product.platform_product_id or "",
                product.listed_price or "",
                product.minimum_price or "未设置",
                "启用" if product.enabled else "停用",
            )
            for column, value in enumerate(values):
                self.product_table.setItem(row, column, QTableWidgetItem(value))

    def _refresh_media_assets(self) -> None:
        self.media_table.setRowCount(0)
        for asset in self.repository.list_media_assets():
            row = self.media_table.rowCount()
            self.media_table.insertRow(row)
            values = (
                asset.asset_id,
                asset.display_name,
                asset.product_key or "通用",
                asset.scene_tag,
                asset.mime_type,
                f"{asset.sha256[:12]}…",
            )
            for column, value in enumerate(values):
                self.media_table.setItem(row, column, QTableWidgetItem(value))

    def _credential_status(self) -> str:
        if not self.credential_store.available:
            return "不可用：未配置安全凭据后端，自动接待不能启用。"
        try:
            return "已配置" if self.credential_store.get("deepseek_api_key") else "未配置"
        except CredentialStoreUnavailableError:
            return "不可用：无法读取安全凭据。"

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.config_status_label.setProperty("tone", "error" if error else "success")
        self.config_status_label.style().unpolish(self.config_status_label)
        self.config_status_label.style().polish(self.config_status_label)
        self.config_status_label.setText(message)
        self.status_changed.emit(message)

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setMinimumHeight(130)


class ProductKnowledgeDialog(QDialog):
    """Edit all user-maintained facts required by the V1 product contract."""

    def __init__(self, product: ProductKnowledge | None = None, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("商品客服知识")
        self.resize(560, 620)
        self.product_key_input = QLineEdit()
        self.name_input = QLineEdit()
        self.aliases_input = QLineEdit()
        self.platform_id_input = QLineEdit()
        self.listed_price_input = QLineEdit()
        self.minimum_price_input = QLineEdit()
        self.specifications_input = QPlainTextEdit()
        self.inventory_input = QPlainTextEdit()
        self.shipping_input = QPlainTextEdit()
        self.after_sales_input = QPlainTextEdit()
        self.supplementary_input = QPlainTextEdit()
        self.enabled_input = QCheckBox("启用该商品知识")
        self.enabled_input.setChecked(True)
        form = QFormLayout()
        form.addRow("商品 Key：", self.product_key_input)
        form.addRow("商品名称：", self.name_input)
        form.addRow("别名（逗号分隔）：", self.aliases_input)
        form.addRow("平台商品 ID：", self.platform_id_input)
        form.addRow("标价：", self.listed_price_input)
        form.addRow("最低可接受价：", self.minimum_price_input)
        for label, widget in (
            ("规格：", self.specifications_input),
            ("库存说明：", self.inventory_input),
            ("发货说明：", self.shipping_input),
            ("售后说明：", self.after_sales_input),
            ("补充知识：", self.supplementary_input),
        ):
            widget.setFixedHeight(52)
            form.addRow(label, widget)
        form.addRow(self.enabled_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        if product is not None:
            self._set_product(product)

    def product(self) -> ProductKnowledge:
        return ProductKnowledge(
            product_key=self.product_key_input.text().strip(),
            name=self.name_input.text().strip(),
            aliases=tuple(alias.strip() for alias in self.aliases_input.text().split(",") if alias.strip()),
            platform_product_id=self.platform_id_input.text().strip() or None,
            listed_price=self.listed_price_input.text().strip() or None,
            minimum_price=self.minimum_price_input.text().strip() or None,
            specifications=self.specifications_input.toPlainText().strip(),
            inventory_notes=self.inventory_input.toPlainText().strip(),
            shipping_notes=self.shipping_input.toPlainText().strip(),
            after_sales_notes=self.after_sales_input.toPlainText().strip(),
            supplementary_knowledge=self.supplementary_input.toPlainText().strip(),
            enabled=self.enabled_input.isChecked(),
        )

    def _accept_if_valid(self) -> None:
        try:
            self.product()
        except ValueError as error:
            QMessageBox.warning(self, "商品知识无效", str(error))
            return
        self.accept()

    def _set_product(self, product: ProductKnowledge) -> None:
        self.product_key_input.setText(product.product_key)
        self.product_key_input.setReadOnly(True)
        self.name_input.setText(product.name)
        self.aliases_input.setText(", ".join(product.aliases))
        self.platform_id_input.setText(product.platform_product_id or "")
        self.listed_price_input.setText(product.listed_price or "")
        self.minimum_price_input.setText(product.minimum_price or "")
        self.specifications_input.setPlainText(product.specifications)
        self.inventory_input.setPlainText(product.inventory_notes)
        self.shipping_input.setPlainText(product.shipping_notes)
        self.after_sales_input.setPlainText(product.after_sales_notes)
        self.supplementary_input.setPlainText(product.supplementary_knowledge)
        self.enabled_input.setChecked(product.enabled)


class MediaAssetDialog(QDialog):
    """Collect safe metadata for one local image before hashing it."""

    def __init__(self, path: Path, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登记本地图片")
        self.product_key_input = QLineEdit()
        self.display_name_input = QLineEdit(path.stem)
        self.scene_tag_input = QLineEdit()
        self.description_input = QPlainTextEdit()
        self.description_input.setFixedHeight(70)
        form = QFormLayout()
        form.addRow("文件：", QLabel(str(path)))
        form.addRow("商品 Key（可空）：", self.product_key_input)
        form.addRow("显示名称：", self.display_name_input)
        form.addRow("场景标签：", self.scene_tag_input)
        form.addRow("给模型的描述：", self.description_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not self.display_name_input.text().strip() or not self.scene_tag_input.text().strip():
            QMessageBox.warning(self, "图片资源无效", "显示名称和场景标签不能为空。")
            return
        self.accept()

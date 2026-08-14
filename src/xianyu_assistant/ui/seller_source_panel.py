"""A compact, guided workflow for importing one seller homepage."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class SellerSourcePanel(QWidget):
    """Keep the first three actions in one restrained import panel."""

    browser_requested = pyqtSignal()
    collect_requested = pyqtSignal(str, int)
    download_requested = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sellerWorkflow")
        self.setStyleSheet(
            """
            QWidget#sellerWorkflow { background: transparent; border: none; }
            QFrame#workflowShell {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
            }
            QFrame#workflowDivider { background: #e2e8f0; max-width: 1px; }
            QLabel#workflowTitle { color: #172b4d; font-size: 20px; font-weight: 700; }
            QLabel#workflowSubtitle { color: #64748b; }
            QLabel#stepCaption { color: #64748b; font-size: 12px; font-weight: 700; }
            QLabel#stepTitle { color: #1e293b; font-size: 15px; font-weight: 700; }
            QLabel#stepHint { color: #64748b; }
            QLabel#progressPill {
                background: #eff6ff;
                border: 1px solid #dbeafe;
                border-radius: 12px;
                color: #2563eb;
                font-size: 12px;
                font-weight: 700;
                padding: 3px 9px;
            }
            QFrame#quantityControl {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
            }
            QFrame#quantityControl:focus-within { border: 2px solid #3b82f6; }
            QPushButton#quantityButton {
                background: transparent;
                border: none;
                border-radius: 6px;
                color: #475569;
                font-size: 18px;
                font-weight: 500;
                min-height: 28px;
                min-width: 28px;
                padding: 0;
            }
            QPushButton#quantityButton:hover { background: #eff6ff; color: #2563eb; }
            QPushButton#quantityButton:disabled { background: transparent; color: #cbd5e1; }
            QSpinBox#quantityInput {
                background: transparent;
                border: none;
                color: #172b4d;
                font-weight: 600;
                min-height: 28px;
                padding: 0 2px;
            }
            QSpinBox#quantityInput:focus { border: none; }
            QLabel#quantityUnit { color: #64748b; }
            """
        )

        self.browser_button = QPushButton("启动浏览器")
        self.browser_button.setObjectName("primaryAction")
        self.browser_status_label = QLabel("请先登录闲鱼，随后即可导入商品。")
        self.browser_status_label.setObjectName("stepHint")
        self.browser_status_label.setWordWrap(True)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴卖家主页链接：https://www.goofish.com/personal?userId=...")
        self.max_products_input = QSpinBox()
        self.max_products_input.setObjectName("quantityInput")
        self.max_products_input.setRange(1, 100)
        self.max_products_input.setValue(30)
        self.max_products_input.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.max_products_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.max_products_input.setFixedWidth(42)
        self.decrease_button = QPushButton("−")
        self.decrease_button.setObjectName("quantityButton")
        self.decrease_button.setToolTip("减少采集数量")
        self.increase_button = QPushButton("+")
        self.increase_button.setObjectName("quantityButton")
        self.increase_button.setToolTip("增加采集数量")
        self.collect_button = QPushButton("开始采集")
        self.collect_button.setObjectName("primaryAction")
        self.status_label = QLabel("每次采集最多 100 件商品。")
        self.status_label.setObjectName("stepHint")
        self.status_label.setWordWrap(True)

        self.download_button = QPushButton("下载图片")
        self.download_button.setObjectName("secondaryAction")
        self.download_button.setEnabled(False)
        self.download_status_label = QLabel("完成采集后可下载当前批次的图片。")
        self.download_status_label.setObjectName("stepHint")
        self.download_status_label.setWordWrap(True)
        self.settings_button = QPushButton("连接设置")
        self.settings_button.setObjectName("linkAction")
        self.settings_button.setFlat(True)

        header = QVBoxLayout()
        header.setSpacing(3)
        title = QLabel("商品导入")
        title.setObjectName("workflowTitle")
        subtitle = QLabel("从卖家主页采集商品，下载图片后即可逐件预填发布页。")
        subtitle.setObjectName("workflowSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle)

        progress = QHBoxLayout()
        progress.setSpacing(8)
        for number, label in (("1", "连接浏览器"), ("2", "导入商品"), ("3", "下载图片"), ("4", "发布辅助")):
            item = QLabel(f"{number}  {label}")
            item.setObjectName("progressPill")
            progress.addWidget(item)
        progress.addStretch()

        workflow = QFrame()
        workflow.setObjectName("workflowShell")
        workflow_layout = QHBoxLayout(workflow)
        workflow_layout.setContentsMargins(16, 14, 16, 14)
        workflow_layout.setSpacing(16)
        workflow_layout.addLayout(self._browser_section(), 2)
        workflow_layout.addWidget(self._divider())
        workflow_layout.addLayout(self._source_section(), 5)
        workflow_layout.addWidget(self._divider())
        workflow_layout.addLayout(self._download_section(), 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addLayout(progress)
        layout.addWidget(workflow)

        self.browser_button.clicked.connect(self.browser_requested)
        self.settings_button.clicked.connect(self.settings_requested)
        self.collect_button.clicked.connect(self._request_collection)
        self.download_button.clicked.connect(self.download_requested)
        self.decrease_button.clicked.connect(self.max_products_input.stepDown)
        self.increase_button.clicked.connect(self.max_products_input.stepUp)
        self.max_products_input.valueChanged.connect(self._update_quantity_buttons)
        self._update_quantity_buttons()

    def _browser_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(6)
        layout.addLayout(self._section_heading("步骤 1", "浏览器"))
        layout.addWidget(self.browser_status_label)
        layout.addStretch()
        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addWidget(self.browser_button)
        controls.addWidget(self.settings_button)
        controls.addStretch()
        layout.addLayout(controls)
        return layout

    def _source_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(7)
        layout.addLayout(self._section_heading("步骤 2", "卖家主页"))
        layout.addWidget(self.url_input)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        count_label = QLabel("采集数量")
        count_label.setStyleSheet("color: #475569;")
        controls.addWidget(count_label)
        controls.addWidget(self._quantity_control())
        controls.addStretch()
        controls.addWidget(self.collect_button)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)
        return layout

    def _quantity_control(self) -> QFrame:
        control = QFrame()
        control.setObjectName("quantityControl")
        control.setFixedHeight(36)
        layout = QHBoxLayout(control)
        layout.setContentsMargins(3, 2, 3, 2)
        layout.setSpacing(1)
        unit_label = QLabel("件")
        unit_label.setObjectName("quantityUnit")
        layout.addWidget(self.decrease_button)
        layout.addWidget(self.max_products_input)
        layout.addWidget(unit_label)
        layout.addWidget(self.increase_button)
        return control

    def _download_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(6)
        layout.addLayout(self._section_heading("步骤 3", "图片"))
        layout.addWidget(self.download_status_label)
        layout.addStretch()
        layout.addWidget(self.download_button, alignment=Qt.AlignmentFlag.AlignLeft)
        return layout

    @staticmethod
    def _section_heading(caption: str, title: str) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(1)
        caption_label = QLabel(caption)
        caption_label.setObjectName("stepCaption")
        title_label = QLabel(title)
        title_label.setObjectName("stepTitle")
        layout.addWidget(caption_label)
        layout.addWidget(title_label)
        return layout

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("workflowDivider")
        return divider

    def focus_url_input(self) -> None:
        """Move keyboard focus to the seller source field."""

        self.url_input.setFocus()

    def set_browser_connecting(self) -> None:
        self.browser_button.setEnabled(False)
        self.browser_button.setText("正在连接…")
        self.browser_status_label.setStyleSheet("color: #64748b;")
        self.browser_status_label.setText("正在启动或复用本机浏览器。")

    def set_browser_ready(self) -> None:
        self.browser_button.setEnabled(True)
        self.browser_button.setText("浏览器已连接")
        self.browser_status_label.setStyleSheet("color: #16803c;")
        self.browser_status_label.setText("已连接闲鱼，可以开始导入。")

    def set_browser_error(self, message: str) -> None:
        self.browser_button.setEnabled(True)
        self.browser_button.setText("重新连接")
        self.browser_status_label.setStyleSheet("color: #dc2626;")
        self.browser_status_label.setText(f"连接失败：{message}")

    def set_collecting(self) -> None:
        """Prevent duplicate imports while the browser worker is active."""

        self.collect_button.setEnabled(False)
        self.collect_button.setText("正在采集…")
        self.url_input.setEnabled(False)
        self._set_quantity_enabled(False)
        self.status_label.setStyleSheet("color: #64748b;")
        self.status_label.setText("正在读取主页并补全商品详情，请稍候。")

    def set_idle(self) -> None:
        """Restore source controls once the active import is finished."""

        self.collect_button.setEnabled(True)
        self.collect_button.setText("开始采集")
        self.url_input.setEnabled(True)
        self._set_quantity_enabled(True)

    def set_error(self, message: str) -> None:
        """Show a validation or collection failure beside its source field."""

        self.status_label.setStyleSheet("color: #dc2626;")
        self.status_label.setText(message)

    def set_result(self, count: int) -> None:
        """Enable the next step after a safe detail import completes."""

        self.status_label.setStyleSheet("color: #16803c;")
        self.status_label.setText(f"已采集 {count} 件商品，可以下载图片。")
        self.download_button.setEnabled(count > 0)
        self.download_status_label.setText("下载当前批次的全部商品图片。")

    def set_downloading(self) -> None:
        self.download_button.setEnabled(False)
        self.download_button.setText("正在下载…")
        self.download_status_label.setStyleSheet("color: #64748b;")
        self.download_status_label.setText("正在下载当前批次图片，请稍候。")

    def set_download_result(self, count: int, errors: bool) -> None:
        self.download_button.setEnabled(True)
        self.download_button.setText("重新下载")
        self.download_status_label.setStyleSheet("color: #dc2626;" if errors else "color: #16803c;")
        if errors:
            self.download_status_label.setText(f"已下载 {count} 张，部分失败。请查看底部提示。")
        else:
            self.download_status_label.setText(f"已下载 {count} 张。可在下方逐件发布辅助。")

    def _request_collection(self) -> None:
        self.collect_requested.emit(self.url_input.text().strip(), self.max_products_input.value())

    def _set_quantity_enabled(self, enabled: bool) -> None:
        self.max_products_input.setEnabled(enabled)
        self.decrease_button.setEnabled(enabled and self.max_products_input.value() > 1)
        self.increase_button.setEnabled(enabled and self.max_products_input.value() < 100)

    def _update_quantity_buttons(self, _value: int | None = None) -> None:
        if not self.max_products_input.isEnabled():
            return
        self.decrease_button.setEnabled(self.max_products_input.value() > 1)
        self.increase_button.setEnabled(self.max_products_input.value() < 100)

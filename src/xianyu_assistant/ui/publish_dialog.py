"""Local product-review dialog used before browser prefill."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from xianyu_assistant.domain.models import ProductRecord
from xianyu_assistant.publishing.xianyu_publisher import PublishDraft


class PublishDialog(QDialog):
    """Require local review before the user asks the app to prefill a browser page."""

    prefill_requested = pyqtSignal(object)

    def __init__(self, product: ProductRecord) -> None:
        super().__init__()
        self._product = product
        self.setWindowTitle("发布前确认")
        self.setMinimumWidth(560)
        self.resize(680, 620)

        self.price_input = QLineEdit(product.price)
        self.description_input = QTextEdit(product.description)
        self.description_preview_label = QLabel()
        self.description_preview_label.setWordWrap(True)
        self.category_input = QLineEdit(product.category)
        self.category_input.setPlaceholderText("请在发布页手动选择分类")
        self.category_path_label = QLabel(self._category_path_summary())
        self.category_path_label.setWordWrap(True)
        self.attributes_label = QLabel(self._attribute_summary())
        self.attributes_label.setWordWrap(True)
        self.images_label = QLabel(self._image_summary())
        warning = QLabel(
            "闲鱼当前发布页没有独立标题栏。程序只会写入采集到的宝贝描述、图片和售价；"
            "图片与描述完成后才等待属性规格出现。分类及最后“发布”均须由你手动确认。"
        )
        warning.setObjectName("warningCard")
        warning.setWordWrap(True)

        form = QFormLayout()
        form.addRow("价格：", self.price_input)
        form.addRow("宝贝描述：", self.description_input)
        form.addRow("描述预览：", self.description_preview_label)
        form.addRow("已采集属性：", self.attributes_label)
        form.addRow("分类路径：", self.category_path_label)
        form.addRow("原始分类：", self.category_input)
        form.addRow("图片：", self.images_label)

        self.prefill_button = QPushButton("预填到发布页（不会发布）")
        self.prefill_button.setObjectName("primaryAction")
        self.prefill_button.clicked.connect(self._request_prefill)
        self.prefill_status_label = QLabel()
        self.prefill_status_label.setWordWrap(True)
        self.prefill_status_label.setVisible(False)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(warning)
        layout.addLayout(form)
        layout.addWidget(self.prefill_button)
        layout.addWidget(self.prefill_status_label)
        layout.addWidget(buttons)

        self.description_input.textChanged.connect(self._update_description_preview)
        self._update_description_preview()

    def draft(self) -> PublishDraft:
        """Return reviewed local values without persisting or submitting them."""
        return PublishDraft(
            product_id=self._product.id,
            price=self.price_input.text().strip(),
            description=self.description_input.toPlainText().strip(),
            category=self.category_input.text().strip(),
            image_paths=tuple(path for path in self._product.image_paths if path),
            attributes=self._product.attributes,
            category_path=self._product.category_path,
            category_ids=self._product.category_ids,
        )

    def _image_summary(self) -> str:
        available = sum(bool(path and Path(path).is_file()) for path in self._product.image_paths)
        return f"已下载 {available}/{len(self._product.image_paths)} 张；预填前必须至少有一张本地图片。"

    def _attribute_summary(self) -> str:
        if not self._product.attributes:
            return "未从详情页识别到属性规格。"
        return "\n".join(
            f"{attribute.name}：{attribute.value}" for attribute in self._product.attributes
        )

    def _category_path_summary(self) -> str:
        if self._product.category_path:
            return " > ".join(self._product.category_path)
        if self._product.category_ids:
            return f"未解析分类名称；详情接口分类 ID：{' > '.join(self._product.category_ids)}"
        return "未从详情页采集到分类路径。"

    def _request_prefill(self) -> None:
        self.set_prefill_in_progress()
        self.prefill_requested.emit(self.draft())

    def _update_description_preview(self) -> None:
        """Show the character budget of the only source text field on the current page."""
        description = self.description_input.toPlainText().strip()
        self.description_preview_label.setText(f"将原样写入宝贝描述：{len(description)}/1500 字")
        self.description_preview_label.setStyleSheet(
            "color: #c62828;" if len(description) > 1500 else ""
        )

    def set_prefill_in_progress(self) -> None:
        """Make the asynchronous browser operation visible in the dialog."""
        self.prefill_button.setEnabled(False)
        self.prefill_button.setText("正在预填，请稍候…")
        self.prefill_status_label.setStyleSheet("")
        self.prefill_status_label.setText(
            "正在连接浏览器并预填。程序会依次写入宝贝描述、图片、价格，再等待属性规格出现；不会点击最终发布。"
        )
        self.prefill_status_label.setVisible(True)

    def set_prefill_failure(self, message: str) -> None:
        """Explain a failed safe prefill and allow the user to try again."""
        self.prefill_button.setEnabled(True)
        self.prefill_button.setText("重试预填到发布页（不会发布）")
        self.prefill_status_label.setStyleSheet("color: #c62828;")
        self.prefill_status_label.setText(f"未预填：{message}")
        self.prefill_status_label.setVisible(True)

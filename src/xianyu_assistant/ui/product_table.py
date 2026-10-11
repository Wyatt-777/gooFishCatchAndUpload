"""Product list presentation for the final, per-item workflow step."""

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

from xianyu_assistant.domain.models import ProductRecord


class ProductTable(QWidget):
    """Make the publish-review action available only after image download."""

    _HEADERS = ("图片", "商品摘要", "价格", "属性", "当前状态", "下一步")
    publish_requested = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("productCatalog")
        self.context_label = QLabel("商品列表")
        self.context_label.setObjectName("productSectionTitle")
        self.helper_label = QLabel("完成上方第 2 步后，导入的商品会显示在这里。")
        self.helper_label.setObjectName("mutedText")

        self.table = QTableWidget(0, len(self._HEADERS))
        self.table.setObjectName("productCatalogTable")
        self.table.setHorizontalHeaderLabels(self._HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        header = self.table.horizontalHeader()
        for column in (0, 2, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        for column in (1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 84), (2, 148), (4, 120), (5, 150)):
            header.resizeSection(column, width)

        self.empty_state = self._build_empty_state()
        self.content = QWidget()
        self.content_layout = QStackedLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.addWidget(self.empty_state)
        self.content_layout.addWidget(self.table)
        self.content_layout.setCurrentWidget(self.empty_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(3)
        layout.addWidget(self.context_label)
        layout.addWidget(self.helper_label)
        layout.addWidget(self.content, 1)

    def _build_empty_state(self) -> QFrame:
        empty_state = QFrame()
        empty_state.setObjectName("emptyState")
        layout = QVBoxLayout(empty_state)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        badge = QLabel("等待导入")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setObjectName("emptyStateBadge")
        title = QLabel("还没有导入商品")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("emptyStateTitle")
        hint = QLabel("先完成上方的「卖家主页」采集，商品会自动出现在这里。")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setObjectName("mutedText")
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)
        return empty_state

    def set_products(self, source_label: str, products: list[ProductRecord]) -> None:
        """Render products for the active collection, including a reopened history batch."""

        self.table.setRowCount(0)
        if not products:
            self.context_label.setText("商品列表")
            self.helper_label.setText(f"当前来源「{source_label}」还没有采集到商品。")
            self.content_layout.setCurrentWidget(self.empty_state)
            return

        self.context_label.setText(f"{source_label} · {len(products)} 件")
        self.helper_label.setText("下载图片后，可逐件点击「发布辅助」预填发布页；最终发布仍由你手动完成。")
        for product in products:
            self._add_product_row(product)
        self.content_layout.setCurrentWidget(self.table)

    def _add_product_row(self, product: ProductRecord) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        image_count = sum(path is not None for path in product.image_paths)
        ready_to_publish = image_count > 0
        values = (
            f"{image_count}/{len(product.image_urls)} 张",
            product.title,
            product.price,
            "；".join(f"{attribute.name}：{attribute.value}" for attribute in product.attributes)
            or "未识别",
            "可发布辅助" if ready_to_publish else "等待下载图片",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (1, 2, 3):
                item.setToolTip(value)
            self.table.setItem(row, column, item)

        publish_button = QPushButton("发布辅助" if ready_to_publish else "先下载图片")
        publish_button.setObjectName("productRowAction")
        publish_button.setFixedHeight(26)
        publish_button.setEnabled(ready_to_publish)
        publish_button.setToolTip("图片下载完成后，可安全预填发布页；不会自动发布。")
        publish_button.clicked.connect(
            lambda _checked=False, item=product: self.publish_requested.emit(item)
        )
        action_cell = QWidget()
        action_layout = QHBoxLayout(action_cell)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(publish_button)
        self.table.setCellWidget(row, len(values), action_cell)

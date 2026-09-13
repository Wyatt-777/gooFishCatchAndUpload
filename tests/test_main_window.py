"""Behaviour checks for the seller-homepage desktop workflow."""

from pathlib import Path

from xianyu_assistant.customer_service.models import (
    PriceChangeDraft,
    PriceChangeStatus,
    ProductKnowledge,
)
from xianyu_assistant.domain.models import ProductCandidate, TaskStatus
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository
from xianyu_assistant.ui.main_window import MainWindow


def _window(database_path: Path) -> MainWindow:
    return MainWindow(SqliteRepository(database_path), auto_start_browser=False)


def test_seller_homepage_validation_is_shown_without_creating_an_internal_collection(
    qapp: object,
    tmp_path: Path,
) -> None:
    """The simplified UI rejects non-profile sources before starting a worker."""

    window = _window(tmp_path / "assistant.db")
    window.seller_source_panel.url_input.setText("https://www.goofish.com/search?q=bike")

    window.seller_source_panel.collect_button.click()

    assert window.current_collection_id is None
    assert "卖家个人主页" in window.seller_source_panel.status_label.text()
    assert window.product_table.context_label.text() == "商品列表"
    assert window.seller_source_panel.download_button.isEnabled() is False


def test_settings_tab_selects_browser_settings(qapp: object, tmp_path: Path) -> None:
    """The browser connection settings remain accessible in the smaller UI."""

    window = _window(tmp_path / "assistant.db")
    window.show_settings()

    assert window.tabs.tabText(window.tabs.currentIndex()) == "浏览器设置"


def test_default_screen_exposes_the_guided_workflow_before_advanced_settings(
    qapp: object,
    tmp_path: Path,
) -> None:
    """Daily actions should stay on the first screen in their natural order."""

    window = _window(tmp_path / "assistant.db")

    assert window.tabs.tabText(0) == "开始处理"
    assert window.seller_source_panel.browser_button.text() == "启动浏览器"
    assert window.seller_source_panel.collect_button.text() == "开始采集"
    assert window.seller_source_panel.download_button.isEnabled() is False


def test_customer_service_tab_exposes_safe_human_review_controls(
    qapp: object,
    tmp_path: Path,
) -> None:
    window = _window(tmp_path / "assistant.db")

    assert window.tabs.tabText(4) == "客服接待"
    assert window.customer_service_panel.mode_combo.currentData().value == "human_confirmation"
    assert window.customer_service_panel.start_button.isEnabled() is True
    assert window.customer_service_panel.stop_button.isEnabled() is False
    assert window.customer_service_panel.confirm_button.isEnabled() is False


def test_main_window_can_be_resized_vertically_with_long_tabs_scrollable(
    qapp: object,
    tmp_path: Path,
) -> None:
    window = _window(tmp_path / "assistant.db")
    window.show()

    window.resize(1160, 760)
    window.tabs.setCurrentWidget(window.customer_service_page)
    qapp.processEvents()  # type: ignore[attr-defined]
    assert window.customer_service_page.verticalScrollBar().isVisible() is False

    window.resize(900, 560)
    qapp.processEvents()  # type: ignore[attr-defined]
    compact_height = window.height()
    window.resize(900, 720)
    qapp.processEvents()  # type: ignore[attr-defined]

    assert window.minimumSizeHint().height() <= 520
    assert compact_height == 560
    assert window.height() == 720


def test_price_change_review_keeps_default_customer_service_view_compact(
    qapp: object,
    tmp_path: Path,
) -> None:
    window = _window(tmp_path / "assistant.db")
    window.customer_service_repository.save_price_change_draft(
        PriceChangeDraft(
            task_id="pc-preview",
            conversation_key="session-preview",
            customer_message_key="message-preview",
            product_key="battery-6020",
            product_name="60V20Ah 铁塔电池",
            customer_offer="410.00",
            proposed_price="418.00",
            minimum_price="400.00",
            listed_price="428.00",
            status=PriceChangeStatus.NEEDS_CONFIGURATION,
            failure_reason="商品未设置价格",
        )
    )
    window.customer_service_repository.save_product_knowledge(
        ProductKnowledge(
            product_key="battery-6020",
            name="60V20Ah 铁塔电池",
            listed_price="428",
            minimum_price="400",
        )
    )
    window.customer_service_panel.refresh_price_changes()
    window.resize(1160, 760)
    window.tabs.setCurrentWidget(window.customer_service_page)
    window.show()
    qapp.processEvents()  # type: ignore[attr-defined]

    assert window.customer_service_page.verticalScrollBar().isVisible() is False
    assert window.customer_service_panel.price_change_table.rowCount() == 1
    assert (
        window.customer_service_repository.list_price_change_drafts()[0].status
        is PriceChangeStatus.AWAITING_REVIEW
    )


def test_history_tab_reopens_a_saved_collection(qapp: object, tmp_path: Path) -> None:
    repository = SqliteRepository(tmp_path / "assistant.db")
    repository.initialize()
    collection = repository.create_task("https://www.goofish.com/personal?userId=42")
    repository.transition_task(collection.id, TaskStatus.RUNNING)
    repository.save_products(
        collection.id,
        [
            ProductCandidate(
                external_id="history-product",
                title="历史公路车",
                price="¥1200",
                description="已保存的商品",
                category="自行车",
                url="https://www.goofish.com/item?id=history-product",
                image_urls=("https://img.example/history.jpg",),
            )
        ],
    )
    repository.transition_task(collection.id, TaskStatus.COMPLETED, progress=1)
    window = MainWindow(repository, auto_start_browser=False)

    window.tabs.setCurrentWidget(window.collection_history_panel)

    assert window.collection_history_panel.table.rowCount() == 1
    assert window.collection_history_panel.table.item(0, 1).text().endswith("userId=42")
    open_button = window.collection_history_panel.table.cellWidget(0, 4)
    assert open_button is not None
    open_button.click()  # type: ignore[attr-defined]

    assert window.current_collection_id == collection.id
    assert window.product_table.context_label.text() == f"历史抓取 #{collection.id} · 1 件"
    assert window.product_table.table.item(0, 1).text() == "历史公路车"
    assert window.seller_source_panel.download_button.isEnabled() is True
    assert window.tabs.currentWidget() is window.workflow_page

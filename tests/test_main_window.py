"""Behaviour checks for the seller-homepage desktop workflow."""

from pathlib import Path

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

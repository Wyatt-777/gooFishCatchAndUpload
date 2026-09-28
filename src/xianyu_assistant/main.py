"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import KeyringCredentialStore
from xianyu_assistant.ui.main_window import MainWindow


def main(argv: Sequence[str] | None = None) -> int:
    """Create the Qt application and display the initial main window."""
    arguments = list(argv) if argv is not None else sys.argv
    if "--self-test" in arguments:
        return _run_packaged_self_test()
    _configure_logging()
    app = QApplication(arguments)
    _configure_application_style(app)
    app.setApplicationName("闲鱼铺货助手")

    window = MainWindow()
    window.show()
    return app.exec()


def _run_packaged_self_test() -> int:
    """Check bundled SQLite and Windows credential dependencies without opening the UI."""
    report_path = os.environ.get("XIANYU_ASSISTANT_SELF_TEST_REPORT")
    try:
        _write_self_test_report(report_path, "started")
        with TemporaryDirectory(prefix="xianyu-assistant-self-test-") as directory:
            repository = CustomerServiceRepository(Path(directory) / "self-test.db")
            repository.initialize()
            _write_self_test_report(report_path, "sqlite-ready")
            if not KeyringCredentialStore().available:
                _write_self_test_report(report_path, "credential-unavailable")
                return 2
            _write_self_test_report(report_path, "credential-ready")
    except Exception:  # noqa: BLE001 - write only exception type/traceback to explicit test path
        if report_path:
            try:
                Path(report_path).write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
        return 1
    _write_self_test_report(report_path, "passed")
    return 0


def _write_self_test_report(path: str | None, message: str) -> None:
    if not path:
        return
    try:
        Path(path).write_text(message, encoding="utf-8")
    except OSError:
        pass


def _configure_application_style(app: QApplication) -> None:
    """Use one light palette instead of inheriting an incompatible OS dark theme."""

    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f4f7fb"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#1b2940"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fd"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#1b2940"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#31435c"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#315fd7"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8997aa"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#98a2b3"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#98a2b3")
    )
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QWidget {
            color: #1b2940;
            font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            font-size: 14px;
        }
        QMainWindow, QDialog, QTabWidget::pane {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #f8faff, stop:1 #f0f4fa);
        }
        QScrollArea, QScrollArea > QWidget > QWidget {
            background: transparent;
            border: none;
        }
        QToolBar {
            background: #ffffff;
            border: none;
            border-bottom: 1px solid #dce5f0;
            padding: 7px 12px;
            spacing: 8px;
        }
        QTabWidget::pane {
            border: none;
            border-top: 1px solid #dce5f0;
        }
        QTabBar { background: #ffffff; }
        QTabBar::tab {
            background: transparent;
            border: none;
            border-bottom: 3px solid transparent;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            color: #65758d;
            margin: 0 3px;
            padding: 13px 17px 11px 17px;
        }
        QTabBar::tab:selected {
            background: #315fd7;
            color: #ffffff;
            border-bottom: 3px solid #244cae;
            font-weight: 700;
        }
        QTabBar::tab:hover { color: #315fd7; background: #f2f5ff; }
        QTabBar::tab:selected:hover { color: #ffffff; background: #284fbc; }
        QGroupBox {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #ffffff, stop:1 #fcfdff);
            border: 1px solid #dce5f0;
            border-radius: 14px;
            font-weight: 700;
            margin-top: 11px;
            padding: 15px 13px 13px 13px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 5px;
            color: #293d5c;
        }
        QGroupBox#receptionControls {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #ffffff, stop:1 #f3f7ff);
            border-color: #c7d8f4;
            border-left: 3px solid #315fd7;
        }
        QGroupBox#receptionControls::title { color: #244cae; }
        QGroupBox#priceChangeCard { border-left: 3px solid #d99b48; }
        QGroupBox#draftStatusCard { border-left: 3px solid #6386dd; }
        QGroupBox#handoffCard { border-left: 3px solid #d98b69; }
        QGroupBox#draftEditCard { border-left: 3px solid #66a691; }
        QGroupBox#browserConnectionCard,
        QGroupBox#modelSettingsCard {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #ffffff, stop:1 #f3f7ff);
            border-color: #c7d8f4;
            border-left: 3px solid #315fd7;
        }
        QGroupBox#browserConnectionCard::title,
        QGroupBox#modelSettingsCard::title { color: #244cae; }
        QGroupBox#localDataCard,
        QGroupBox#firstContactCard,
        QGroupBox#productKnowledgeCard { border-left: 3px solid #66a691; }
        QGroupBox#priceFollowUpCard,
        QGroupBox#historyImportCard { border-left: 3px solid #d99b48; }
        QGroupBox#mediaAssetsCard,
        QGroupBox#knowledgeImportCard { border-left: 3px solid #6386dd; }
        QGroupBox#maintenanceCard { border-left: 3px solid #9aaac1; }
        QLabel { background: transparent; }
        QLabel#pageTitle, QLabel#workflowTitle, QLabel#historyTitle {
            color: #1b2940;
            font-size: 21px;
            font-weight: 700;
        }
        QLabel#productSectionTitle {
            color: #293d5c;
            font-size: 17px;
            font-weight: 700;
        }
        QLabel#emptyStateTitle {
            color: #34445a;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#emptyStateBadge {
            background: #eaf1ff;
            border: 1px solid #c6d8fa;
            border-radius: 10px;
            color: #2854ba;
            font-size: 12px;
            font-weight: 700;
            padding: 3px 10px;
        }
        QLabel#pageSubtitle, QLabel#workflowSubtitle, QLabel#mutedText,
        QLabel#sectionHint, QLabel#stepHint {
            color: #64758d;
        }
        QLabel#receptionStatus {
            background: #f0f4f9;
            border: 1px solid #dce5f0;
            border-left: 3px solid #9aadc9;
            border-radius: 8px;
            color: #5e7089;
            font-weight: 600;
            padding: 3px 8px;
        }
        QLabel#receptionStatus[tone="success"] {
            background: #eaf8f2;
            border-color: #b7e5d0;
            border-left-color: #157a55;
            color: #157a55;
        }
        QLabel#receptionStatus[tone="error"] {
            background: #fff1f2;
            border-color: #f5c8ce;
            border-left-color: #bc3948;
            color: #bc3948;
        }
        QLabel#handoffSummary {
            background: #fff9ed;
            border: 1px solid #f4dfb5;
            border-radius: 8px;
            color: #7b5b20;
            padding: 4px 8px;
        }
        QLabel#settingsStatus { color: #64758d; }
        QLabel#settingsStatus[tone="success"] { color: #157a55; }
        QLabel#settingsStatus[tone="error"] { color: #bc3948; }
        QLabel#browserConnectionStatus {
            color: #5e708b;
            font-weight: 600;
        }
        QLabel#browserConnectionStatus[tone="success"] { color: #157a55; }
        QLabel#browserConnectionStatus[tone="error"] { color: #bc3948; }
        QLabel#publishPreviewMeta { color: #526681; }
        QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {
            background: #ffffff;
            border: 1px solid #cbd7e7;
            border-radius: 9px;
            color: #1b2940;
            min-height: 30px;
            padding: 3px 9px;
            selection-background-color: #dce7ff;
        }
        QLineEdit:hover, QSpinBox:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover {
            border-color: #91a8ce;
        }
        QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {
            background: #ffffff;
            border: 2px solid #416ee0;
            padding: 2px 8px;
        }
        QLineEdit:disabled, QSpinBox:disabled, QPlainTextEdit:disabled,
        QTextEdit:disabled, QComboBox:disabled {
            background: #f0f3f8;
            border-color: #e2e7ef;
            color: #97a3b4;
        }
        QPlainTextEdit#infoPanel {
            background: #f7f9fd;
            border: 1px solid #dce5f0;
            color: #506279;
            padding: 9px 11px;
        }
        QComboBox::drop-down {
            border: none;
            width: 26px;
        }
        QComboBox::down-arrow {
            width: 8px;
            height: 8px;
        }
        QPushButton {
            background: #ffffff;
            border: 1px solid #cbd7e7;
            border-radius: 8px;
            color: #31435c;
            font-weight: 600;
            min-height: 30px;
            padding: 3px 12px;
        }
        QPushButton:hover {
            background: #f4f7fd;
            border-color: #91a8ce;
            color: #203653;
        }
        QPushButton:pressed { background: #e9eef9; border-color: #7f9ac6; }
        QPushButton#primaryAction {
            background: #315fd7;
            border-color: #315fd7;
            color: #ffffff;
        }
        QPushButton#primaryAction:hover {
            background: #284fbc;
            border-color: #284fbc;
        }
        QPushButton#primaryAction:pressed { background: #20439f; }
        QPushButton#secondaryAction {
            background: #eef3ff;
            border-color: #c9d8fa;
            color: #2854ba;
        }
        QPushButton#secondaryAction:hover { background: #e3ebff; border-color: #a9bff0; }
        QPushButton#linkAction {
            background: transparent;
            border: none;
            color: #5e7089;
            font-weight: 500;
            padding: 2px 4px;
        }
        QPushButton#linkAction:hover { background: transparent; color: #315fd7; }
        QPushButton#dangerAction {
            background: #fff7f7;
            border-color: #efb5b5;
            color: #b42318;
        }
        QPushButton#dangerAction:hover {
            background: #fff0f0;
            border-color: #dc8d8d;
            color: #912018;
        }
        QPushButton#dangerAction:pressed { background: #ffe3e3; }
        QPushButton:disabled {
            background: #edf1f6;
            border-color: #e1e6ed;
            color: #9aa6b6;
        }
        QTableWidget {
            background: #ffffff;
            alternate-background-color: #f7f9fd;
            border: 1px solid #dce5f0;
            border-radius: 10px;
            gridline-color: transparent;
            selection-background-color: #e1eaff;
            selection-color: #1b2940;
            outline: 0;
        }
        QTableWidget#collectionHistoryTable,
        QTableWidget#productCatalogTable { border-color: #c8d8f0; }
        QTableWidget::item {
            border-bottom: 1px solid #edf1f6;
            padding: 6px 8px;
        }
        QTableWidget::item:hover { background: #f1f5ff; }
        QTableCornerButton::section {
            background: #f3f6fb;
            border: none;
            border-bottom: 1px solid #dce5f0;
        }
        QSplitter#customerServiceWorkspace::handle {
            background: #dce5f0;
            border-radius: 2px;
            margin: 10px 2px;
        }
        QSplitter#customerServiceWorkspace::handle:hover { background: #a9bcda; }
        QFrame#emptyState {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #ffffff, stop:1 #f5f8ff);
            border: 1px dashed #b9cbe3;
            border-radius: 12px;
        }
        QWidget#collectionHistory QFrame#emptyState,
        QWidget#productCatalog QFrame#emptyState {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #fafdff, stop:1 #eef4ff);
            border: 1px solid #d6e3f7;
        }
        QFrame#tableEmptyState {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #ffffff, stop:1 #f5f8ff);
            border: 1px dashed #c5d3e7;
            border-radius: 10px;
        }
        QWidget#customerServicePanel QFrame#tableEmptyState {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #fafdff, stop:1 #f0f5ff);
            border: 1px solid #d9e4f6;
        }
        QWidget#customerServicePanel QLabel#tableEmptyTitle {
            color: #294b85;
        }
        QWidget#customerServicePanel QLabel#sectionHint,
        QWidget#customerServicePanel QLabel#mutedText {
            color: #5e708b;
        }
        QLabel#tableEmptyTitle {
            color: #40516a;
            font-size: 15px;
            font-weight: 700;
        }
        QLabel#warningCard {
            background: #fff9ed;
            border: 1px solid #f4dfb5;
            border-radius: 10px;
            color: #72551f;
            padding: 9px 11px;
        }
        QHeaderView::section {
            background: #f3f6fb;
            border: none;
            border-bottom: 1px solid #dce5f0;
            color: #4f6077;
            font-weight: 700;
            padding: 10px 7px;
        }
        QStatusBar {
            background: #ffffff;
            border-top: 1px solid #dce5f0;
            color: #64758d;
            padding: 2px 8px;
        }
        QScrollBar:vertical { background: transparent; width: 9px; margin: 3px 2px; }
        QScrollBar::handle:vertical {
            background: #bdcadc;
            border-radius: 4px;
            min-height: 28px;
        }
        QScrollBar::handle:vertical:hover { background: #91a7c5; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px 3px; }
        QScrollBar::handle:horizontal {
            background: #bdcadc;
            border-radius: 4px;
            min-width: 28px;
        }
        QScrollBar::handle:horizontal:hover { background: #91a7c5; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QCheckBox { spacing: 7px; color: #40516a; }
        QCheckBox:hover { color: #244cae; }
        QToolTip {
            background: #172033;
            border: 1px solid #172033;
            border-radius: 6px;
            color: #ffffff;
            padding: 5px 7px;
        }
        QMenu {
            background: #ffffff;
            border: 1px solid #dce5f0;
            border-radius: 8px;
            padding: 5px;
        }
        QMenu::item { border-radius: 5px; padding: 7px 22px 7px 10px; }
        QMenu::item:selected { background: #edf2ff; color: #244cae; }
        """
    )


def _configure_logging() -> None:
    """Keep collection diagnostics in the local app-data directory for Debug runs."""
    logger = logging.getLogger("xianyu_assistant")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_directory = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "XianyuAssistant"
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_directory / "xianyu_assistant.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())

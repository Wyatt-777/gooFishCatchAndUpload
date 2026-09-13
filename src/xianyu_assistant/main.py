"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Sequence
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
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f6fb"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#172033"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f8fafd"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#172033"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#27364b"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2f6fed"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8b98aa"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#98a2b3"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#98a2b3")
    )
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QWidget {
            color: #172033;
            font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            font-size: 14px;
        }
        QMainWindow, QDialog, QTabWidget::pane {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #f7f9fd, stop:1 #eef3fa);
        }
        QScrollArea, QScrollArea > QWidget > QWidget {
            background: transparent;
            border: none;
        }
        QToolBar {
            background: #ffffff;
            border: none;
            border-bottom: 1px solid #dfe6f0;
            padding: 7px 12px;
            spacing: 8px;
        }
        QTabWidget::pane {
            border: none;
            border-top: 1px solid #dfe6f0;
        }
        QTabBar { background: #ffffff; }
        QTabBar::tab {
            background: transparent;
            border: none;
            border-bottom: 3px solid transparent;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            color: #66758b;
            margin: 0 3px;
            padding: 13px 17px 11px 17px;
        }
        QTabBar::tab:selected {
            background: #f4f7ff;
            color: #245bc6;
            border-bottom: 3px solid #2f6fed;
            font-weight: 700;
        }
        QTabBar::tab:hover { color: #2f6fed; background: #f5f8ff; }
        QGroupBox {
            background: #ffffff;
            border: 1px solid #dce4ef;
            border-radius: 12px;
            font-weight: 700;
            margin-top: 11px;
            padding: 15px 13px 13px 13px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 5px;
            color: #27364b;
        }
        QLabel { background: transparent; }
        QLabel#pageTitle, QLabel#workflowTitle, QLabel#historyTitle {
            color: #172033;
            font-size: 21px;
            font-weight: 700;
        }
        QLabel#productSectionTitle {
            color: #27364b;
            font-size: 17px;
            font-weight: 700;
        }
        QLabel#emptyStateTitle {
            color: #34445a;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#emptyStateBadge {
            background: #edf4ff;
            border: 1px solid #cfddf7;
            border-radius: 10px;
            color: #376bc5;
            font-size: 12px;
            font-weight: 700;
            padding: 3px 10px;
        }
        QLabel#pageSubtitle, QLabel#workflowSubtitle, QLabel#mutedText,
        QLabel#sectionHint, QLabel#stepHint {
            color: #68778c;
        }
        QLabel#receptionStatus {
            background: #f1f5fa;
            border: 1px solid #dce4ef;
            border-radius: 8px;
            color: #5e7089;
            font-weight: 600;
            padding: 3px 8px;
        }
        QLabel#receptionStatus[tone="success"] {
            background: #edf9f3;
            border-color: #bee7cf;
            color: #18794e;
        }
        QLabel#receptionStatus[tone="error"] {
            background: #fff1f2;
            border-color: #fecdd3;
            color: #c93645;
        }
        QLabel#handoffSummary {
            background: #fff9ed;
            border: 1px solid #f4dfb5;
            border-radius: 8px;
            color: #7b5b20;
            padding: 4px 8px;
        }
        QLabel#settingsStatus { color: #68778c; }
        QLabel#settingsStatus[tone="success"] { color: #18794e; }
        QLabel#settingsStatus[tone="error"] { color: #c93645; }
        QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox {
            background: #fbfdff;
            border: 1px solid #cbd6e4;
            border-radius: 8px;
            color: #172033;
            min-height: 30px;
            padding: 3px 9px;
            selection-background-color: #cfe0ff;
        }
        QLineEdit:hover, QSpinBox:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover {
            border-color: #9fb2ca;
        }
        QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {
            background: #ffffff;
            border: 2px solid #4b82ee;
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
            border: 1px solid #dce4ef;
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
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #ffffff, stop:1 #f7f9fc);
            border: 1px solid #cbd6e4;
            border-radius: 8px;
            color: #34445a;
            font-weight: 600;
            min-height: 30px;
            padding: 3px 12px;
        }
        QPushButton:hover {
            background: #f2f6fc;
            border-color: #9fb2ca;
            color: #22324a;
        }
        QPushButton:pressed { background: #e8eef7; border-color: #8da3bf; }
        QPushButton#primaryAction {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #4b82ee, stop:1 #2f6fed);
            border-color: #2f6fed;
            color: #ffffff;
        }
        QPushButton#primaryAction:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #4077e4, stop:1 #275fcf);
            border-color: #275fcf;
        }
        QPushButton#primaryAction:pressed { background: #2458c2; }
        QPushButton#secondaryAction {
            background: #edf4ff;
            border-color: #c7d9fa;
            color: #285fc9;
        }
        QPushButton#secondaryAction:hover { background: #dfebff; border-color: #9fbdf5; }
        QPushButton#linkAction {
            background: transparent;
            border: none;
            color: #5e7089;
            font-weight: 500;
            padding: 2px 4px;
        }
        QPushButton#linkAction:hover { background: transparent; color: #2f6fed; }
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
            alternate-background-color: #f8fafd;
            border: 1px solid #dce4ef;
            border-radius: 10px;
            gridline-color: transparent;
            selection-background-color: #dfeaff;
            selection-color: #172033;
            outline: 0;
        }
        QTableWidget::item {
            border-bottom: 1px solid #edf1f6;
            padding: 6px 8px;
        }
        QTableWidget::item:hover { background: #f1f6ff; }
        QTableCornerButton::section {
            background: #f5f8fc;
            border: none;
            border-bottom: 1px solid #dce4ef;
        }
        QSplitter#customerServiceWorkspace::handle {
            background: #dfe6f0;
            border-radius: 2px;
            margin: 10px 2px;
        }
        QSplitter#customerServiceWorkspace::handle:hover { background: #b8c8dc; }
        QFrame#emptyState {
            background: #fbfdff;
            border: 1px dashed #bdcadb;
            border-radius: 12px;
        }
        QFrame#tableEmptyState {
            background: #fbfdff;
            border: 1px dashed #cbd6e4;
            border-radius: 10px;
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
            background: #f5f8fc;
            border: none;
            border-bottom: 1px solid #dce4ef;
            color: #4f6077;
            font-weight: 700;
            padding: 10px 7px;
        }
        QStatusBar {
            background: #ffffff;
            border-top: 1px solid #dfe6f0;
            color: #68778c;
            padding: 2px 8px;
        }
        QScrollBar:vertical { background: transparent; width: 9px; margin: 3px 2px; }
        QScrollBar::handle:vertical {
            background: #c6d1df;
            border-radius: 4px;
            min-height: 28px;
        }
        QScrollBar::handle:vertical:hover { background: #9fb0c5; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px 3px; }
        QScrollBar::handle:horizontal {
            background: #c6d1df;
            border-radius: 4px;
            min-width: 28px;
        }
        QScrollBar::handle:horizontal:hover { background: #9fb0c5; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QCheckBox { spacing: 7px; color: #40516a; }
        QToolTip {
            background: #172033;
            border: 1px solid #172033;
            border-radius: 6px;
            color: #ffffff;
            padding: 5px 7px;
        }
        QMenu {
            background: #ffffff;
            border: 1px solid #dce4ef;
            border-radius: 8px;
            padding: 5px;
        }
        QMenu::item { border-radius: 5px; padding: 7px 22px 7px 10px; }
        QMenu::item:selected { background: #edf4ff; color: #245bc6; }
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
        handler: logging.Handler = logging.FileHandler(
            log_directory / "xianyu_assistant.log",
            encoding="utf-8",
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())

"""Application entry point."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from xianyu_assistant.ui.main_window import MainWindow


def main(argv: Sequence[str] | None = None) -> int:
    """Create the Qt application and display the initial main window."""
    _configure_logging()
    app = QApplication(list(argv) if argv is not None else sys.argv)
    _configure_application_style(app)
    app.setApplicationName("闲鱼铺货助手")

    window = MainWindow()
    window.show()
    return app.exec()


def _configure_application_style(app: QApplication) -> None:
    """Use one light palette instead of inheriting an incompatible OS dark theme."""

    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f6f8fc"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#172b4d"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f8fafc"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#172b4d"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#172b4d"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#7b8794"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#98a2b3"))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#98a2b3")
    )
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QWidget {
            color: #172b4d;
            font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            font-size: 14px;
        }
        QMainWindow, QTabWidget::pane { background: #f6f8fc; }
        QToolBar {
            background: #ffffff;
            border: none;
            border-bottom: 1px solid #e2e8f0;
            padding: 6px 10px;
            spacing: 8px;
        }
        QTabBar::tab {
            background: transparent;
            border: none;
            color: #64748b;
            margin: 0 4px;
            padding: 10px 16px;
        }
        QTabBar::tab:selected {
            color: #1d4ed8;
            border-bottom: 3px solid #2563eb;
            font-weight: 700;
        }
        QTabBar::tab:hover { color: #1d4ed8; }
        QGroupBox {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            font-weight: 700;
            margin-top: 10px;
            padding: 14px 12px 12px 12px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
            color: #334155;
        }
        QLabel { background: transparent; }
        QLineEdit, QSpinBox, QPlainTextEdit {
            background: #ffffff;
            border: 1px solid #cbd5e1;
            border-radius: 7px;
            color: #172b4d;
            min-height: 30px;
            padding: 2px 8px;
            selection-background-color: #bfdbfe;
        }
        QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {
            border: 2px solid #3b82f6;
        }
        QPushButton {
            background: #ffffff;
            border: 1px solid #cbd5e1;
            border-radius: 7px;
            color: #334155;
            font-weight: 600;
            min-height: 30px;
            padding: 3px 11px;
        }
        QPushButton:hover { background: #f1f5f9; border-color: #94a3b8; }
        QPushButton:pressed { background: #e2e8f0; }
        QPushButton#primaryAction {
            background: #2563eb;
            border-color: #2563eb;
            color: #ffffff;
        }
        QPushButton#primaryAction:hover { background: #1d4ed8; border-color: #1d4ed8; }
        QPushButton#secondaryAction {
            background: #eff6ff;
            border-color: #bfdbfe;
            color: #1d4ed8;
        }
        QPushButton#secondaryAction:hover { background: #dbeafe; border-color: #93c5fd; }
        QPushButton#linkAction {
            background: transparent;
            border: none;
            color: #64748b;
            font-weight: 500;
            padding: 2px 4px;
        }
        QPushButton#linkAction:hover { background: transparent; color: #2563eb; }
        QPushButton:disabled {
            background: #e8edf5;
            border-color: #e8edf5;
            color: #98a2b3;
        }
        QTableWidget {
            background: #ffffff;
            alternate-background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            gridline-color: #e2e8f0;
            selection-background-color: #dbeafe;
            selection-color: #172b4d;
        }
        QFrame#emptyState {
            background: #ffffff;
            border: 1px dashed #cbd5e1;
            border-radius: 10px;
        }
        QHeaderView::section {
            background: #f1f5f9;
            border: none;
            border-bottom: 1px solid #dbe2ea;
            color: #475569;
            font-weight: 700;
            padding: 9px 6px;
        }
        QStatusBar {
            background: #ffffff;
            border-top: 1px solid #e2e8f0;
            color: #64748b;
        }
        QScrollBar:vertical { background: #f1f5f9; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 5px; min-height: 24px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
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

"""Settings UI for attaching to a user-started Chrome or Edge browser."""

import os
from typing import Protocol

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from xianyu_assistant.browser.manager import DEFAULT_CDP_PORT, BrowserConnectionConfig


class BrowserSettingsStore(Protocol):
    """Minimal persistent settings boundary used by the browser panel."""

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        """Read one setting."""

    def set_setting(self, name: str, value: str) -> None:
        """Persist one setting."""


class BrowserConnectionPanel(QGroupBox):
    """Collect the local CDP port and communicate the secure setup steps."""

    connect_requested = pyqtSignal(object)
    launch_requested = pyqtSignal(object)

    def __init__(self, settings_store: BrowserSettingsStore | None = None) -> None:
        super().__init__("浏览器连接（Chrome / Edge）")
        self._settings_store = settings_store
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65_535)
        self.port_input.setValue(_load_cdp_port(settings_store))
        self.port_input.setAccessibleName("CDP 调试端口")

        self.status_label = QLabel("未连接")
        self.launch_button = QPushButton("启动 Chrome 并连接")
        self.launch_button.setObjectName("primaryAction")
        self.connect_button = QPushButton("连接并打开闲鱼")
        self.connect_button.setObjectName("secondaryAction")
        self.save_port_button = QPushButton("保存端口")

        form = QFormLayout()
        form.addRow("连接地址：", QLabel("localhost（仅本机）"))
        form.addRow("CDP 端口：", self.port_input)
        form.addRow("连接状态：", self.status_label)

        instructions = QPlainTextEdit()
        instructions.setObjectName("infoPanel")
        instructions.setReadOnly(True)
        instructions.setPlainText(
            "程序启动时会自动打开独立的 Chrome/Edge 调试窗口并连接闲鱼。\n"
            "首次使用请在该窗口自行登录闲鱼；登录状态会保存在应用专用目录。\n"
            "CDP 端口始终只监听 localhost，不会暴露到局域网或互联网。\n\n"
            "若自动启动失败，可点击“启动 Chrome 并连接”重试。\n"
            "Chrome 136+ 使用独立的 --user-data-dir，不影响你的日常浏览器配置。"
        )
        instructions.setFixedHeight(144)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("连接说明："))
        layout.addWidget(instructions)
        button_row = QHBoxLayout()
        button_row.addWidget(self.launch_button)
        button_row.addWidget(self.connect_button)
        button_row.addWidget(self.save_port_button)
        layout.addLayout(button_row)

        self.launch_button.clicked.connect(self._emit_launch_request)
        self.connect_button.clicked.connect(self._emit_connect_request)
        self.save_port_button.clicked.connect(self.save_connection_settings)

    def _emit_launch_request(self) -> None:
        self.launch_requested.emit(self.connection_config())

    def _emit_connect_request(self) -> None:
        self.connect_requested.emit(self.connection_config())

    def connection_config(self) -> BrowserConnectionConfig:
        """Return the currently selected safe local CDP configuration."""
        self.save_connection_settings(show_status=False)
        return BrowserConnectionConfig(port=self.port_input.value())

    def save_connection_settings(self, *, show_status: bool = True) -> None:
        """Persist the selected CDP port so every workflow uses the same value."""
        if self._settings_store is not None:
            self._settings_store.set_setting("browser_cdp_port", str(self.port_input.value()))
        if show_status:
            self.status_label.setText(f"端口已保存：{self.port_input.value()}")

    def set_connecting(self) -> None:
        """Prevent duplicate attempts while a worker is contacting the browser."""
        self.launch_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status_label.setText("正在连接…")

    def set_idle(self) -> None:
        """Allow a new connection attempt after the worker finishes."""
        self.launch_button.setEnabled(True)
        self.connect_button.setEnabled(True)

    def set_launching(self) -> None:
        """Show that the application is preparing the managed browser profile."""
        self.launch_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status_label.setText("正在启动 Chrome…")

    def set_connected(self, page_url: str) -> None:
        """Show the final page address returned by the browser."""
        self.status_label.setText(f"已连接：{page_url}")

    def set_connection_error(self, message: str) -> None:
        """Display an actionable connection error without a modal interruption."""
        self.status_label.setText(message)


def _default_cdp_port() -> int:
    """Return the current managed-browser fallback when no persisted value exists."""
    value = os.environ.get("XIANYU_CDP_PORT", str(DEFAULT_CDP_PORT))
    try:
        port = int(value)
    except ValueError:
        return DEFAULT_CDP_PORT
    return port if 1 <= port <= 65_535 else DEFAULT_CDP_PORT


def _load_cdp_port(settings_store: BrowserSettingsStore | None) -> int:
    """Read the persisted port, migrating the legacy environment override once."""
    if settings_store is not None:
        stored = settings_store.get_setting("browser_cdp_port")
        if stored is not None:
            try:
                port = int(stored)
            except ValueError:
                port = 0
            if 1 <= port <= 65_535:
                return port
        port = _default_cdp_port()
        settings_store.set_setting("browser_cdp_port", str(port))
        return port
    return _default_cdp_port()

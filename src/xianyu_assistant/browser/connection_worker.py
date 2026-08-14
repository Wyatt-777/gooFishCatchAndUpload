"""Background worker for one browser connection operation."""

from __future__ import annotations

from time import sleep

from PyQt6.QtCore import QThread, pyqtSignal

from xianyu_assistant.browser.manager import (
    BrowserConnectionConfig,
    BrowserConnectionError,
    BrowserManager,
)


class BrowserConnectionWorker(QThread):
    """Connect to a CDP browser and navigate it without blocking the Qt UI thread."""

    connection_succeeded = pyqtSignal(str)
    connection_failed = pyqtSignal(str)

    def __init__(
        self,
        config: BrowserConnectionConfig,
        *,
        attempts: int = 1,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        super().__init__()
        self._config = config
        self._attempts = max(1, attempts)
        self._retry_delay_seconds = retry_delay_seconds

    def run(self) -> None:
        """Open Xianyu, then release only the automation connection."""
        last_error: BrowserConnectionError | None = None
        for attempt in range(self._attempts):
            manager = BrowserManager(self._config)
            try:
                manager.connect()
                page_url = manager.open_xianyu()
            except BrowserConnectionError as error:
                last_error = error
                if attempt + 1 < self._attempts:
                    sleep(self._retry_delay_seconds)
                    continue
            else:
                self.connection_succeeded.emit(page_url)
                return
            finally:
                # This disconnects Playwright only. It never sends Browser.close()
                # to the Chrome/Edge process owned by the user.
                manager.disconnect()

        assert last_error is not None
        self.connection_failed.emit(str(last_error))

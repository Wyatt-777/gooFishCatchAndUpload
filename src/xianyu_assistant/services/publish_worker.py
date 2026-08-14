"""Background worker for user-triggered publish-page prefill only."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from xianyu_assistant.browser.manager import BrowserConnectionConfig, BrowserManager
from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.publishing.xianyu_publisher import (
    PublishDraft,
    PublishPreparationError,
    XianyuPublisher,
)


class PublishWorker(QThread):
    """Keep browser prefill off the UI thread and never submit a publish form."""

    prepared = pyqtSignal(str, str, int, int, int)
    failed = pyqtSignal(str)

    def __init__(self, draft: PublishDraft, browser_config: BrowserConnectionConfig) -> None:
        super().__init__()
        self._draft = draft
        self._browser_config = browser_config

    def run(self) -> None:
        try:
            result = XianyuPublisher(
                BrowserManager(self._browser_config),
                debug_writer=DebugArtifactWriter(),
            ).prepare(self._draft)
        except PublishPreparationError as error:
            self.failed.emit(str(error))
            return
        self.prepared.emit(
            result.page_url,
            result.category,
            result.uploaded_image_count,
            len(result.filled_attributes),
            len(result.pending_attributes),
        )

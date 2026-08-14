"""Background worker for batch product-image downloads."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from xianyu_assistant.images.image_manager import ImageManager
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository


class ImageDownloadWorker(QThread):
    """Download every image for one task without blocking the UI."""

    completed = pyqtSignal(int, int, str)

    def __init__(self, task_id: int, repository: SqliteRepository, output_directory: Path) -> None:
        super().__init__()
        self._task_id = task_id
        self._repository = repository
        self._output_directory = output_directory

    def run(self) -> None:
        """Keep downloading after individual image errors and summarize the result."""
        manager = ImageManager(self._repository, self._output_directory)
        downloaded = 0
        errors: list[str] = []
        for product in self._repository.list_products(self._task_id):
            summary = manager.download_product_images(product)
            downloaded += summary.downloaded
            errors.extend(summary.errors)
        self.completed.emit(self._task_id, downloaded, "；".join(errors))

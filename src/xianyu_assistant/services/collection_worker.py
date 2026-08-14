"""Qt worker that imports one seller homepage into a local collection session."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from xianyu_assistant.browser.manager import BrowserConnectionConfig, BrowserManager
from xianyu_assistant.crawler.xianyu_crawler import XianyuCrawler, XianyuCrawlerError
from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.domain.models import TaskStatus
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository


class CollectionWorker(QThread):
    """Collect a bounded seller homepage in a background thread and persist it."""

    collection_completed = pyqtSignal(int, int)
    collection_failed = pyqtSignal(int, str)

    def __init__(
        self,
        task_id: int,
        seller_profile_url: str,
        max_products: int,
        repository: SqliteRepository,
        browser_config: BrowserConnectionConfig,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._seller_profile_url = seller_profile_url
        self._max_products = max_products
        self._repository = repository
        self._browser_config = browser_config

    def run(self) -> None:
        """Collect, deduplicate, persist, and report one seller import."""
        try:
            products = XianyuCrawler(
                BrowserManager(self._browser_config),
                debug_writer=DebugArtifactWriter(),
            ).collect_seller_profile(self._seller_profile_url, max_products=self._max_products)
            inserted_count = self._repository.save_products(self._task_id, products)
            self._repository.transition_task(
                self._task_id,
                TaskStatus.COMPLETED,
                progress=len(products),
            )
        except XianyuCrawlerError as error:
            self._repository.transition_task(
                self._task_id,
                TaskStatus.FAILED,
                error_message=str(error),
            )
            self.collection_failed.emit(self._task_id, str(error))
        else:
            self.collection_completed.emit(self._task_id, inserted_count)

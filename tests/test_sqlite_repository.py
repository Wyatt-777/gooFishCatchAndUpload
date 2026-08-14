"""SQLite persistence and task state-machine checks."""

from pathlib import Path

import pytest

from xianyu_assistant.domain.models import ProductCandidate, TaskStatus
from xianyu_assistant.domain.task_state import TaskTransitionError
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository


def _repository(database_path: Path) -> SqliteRepository:
    repository = SqliteRepository(database_path)
    repository.initialize()
    return repository


def _product() -> ProductCandidate:
    return ProductCandidate(
        external_id="product-001",
        title="公路自行车轮组",
        price="¥899",
        description="测试描述",
        category="自行车配件",
        url="https://www.goofish.com/item/product-001",
        image_urls=("https://img.example/one.jpg", "https://img.example/two.jpg"),
    )


def test_products_are_deduplicated_and_keep_all_images(tmp_path: Path) -> None:
    """The same result must update rather than duplicate a task's product record."""
    repository = _repository(tmp_path / "assistant.db")
    task = repository.create_task("公路自行车")

    assert repository.save_products(task.id, [_product()]) == 1
    assert repository.save_products(task.id, [_product()]) == 0

    products = repository.list_products(task.id)
    assert len(products) == 1
    assert products[0].title == "公路自行车轮组"
    assert products[0].image_urls == (
        "https://img.example/one.jpg",
        "https://img.example/two.jpg",
    )


def test_repository_persists_named_category_paths_and_raw_ids(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "assistant.db")
    task = repository.create_task("电动车电池")
    product = ProductCandidate(
        external_id="category-001",
        title="锂电池",
        price="¥500",
        description="测试分类路径",
        category="锂电池",
        url="https://www.goofish.com/item?id=category-001",
        image_urls=("https://img.example/battery.jpg",),
        category_path=("电动车配件", "电池", "锂电池"),
        category_ids=("100", "200", "300"),
    )

    repository.save_products(task.id, [product])

    saved = repository.list_products(task.id)[0]
    assert saved.category_path == ("电动车配件", "电池", "锂电池")
    assert saved.category_ids == ("100", "200", "300")


def test_task_lifecycle_allows_pause_resume_and_rejects_invalid_transition(tmp_path: Path) -> None:
    """State changes preserve a recoverable task lifecycle."""
    repository = _repository(tmp_path / "assistant.db")
    task = repository.create_task("山地自行车")

    task = repository.transition_task(task.id, TaskStatus.RUNNING)
    task = repository.transition_task(task.id, TaskStatus.PAUSED)
    task = repository.transition_task(task.id, TaskStatus.RUNNING)
    task = repository.transition_task(task.id, TaskStatus.COMPLETED, progress=3)

    assert task.status == TaskStatus.COMPLETED
    assert task.progress == 3
    with pytest.raises(TaskTransitionError):
        repository.transition_task(task.id, TaskStatus.RUNNING)


def test_repository_releases_the_database_file_after_each_operation(tmp_path: Path) -> None:
    """Windows callers can immediately clean up a database after repository work."""
    database_path = tmp_path / "release-check.db"
    repository = SqliteRepository(database_path)

    repository.initialize()
    repository.create_task("release-check")

    database_path.unlink()

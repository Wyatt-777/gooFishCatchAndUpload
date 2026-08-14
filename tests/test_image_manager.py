"""Image download and local-path persistence tests."""

from pathlib import Path

from xianyu_assistant.domain.models import ProductCandidate
from xianyu_assistant.images.image_manager import DownloadedImage, ImageManager
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository


class FakeImageFetcher:
    """Avoid external network access while simulating one partial failure."""

    def fetch(self, source_url: str) -> DownloadedImage:
        if source_url.endswith("broken.jpg"):
            raise ValueError("模拟网络失败")
        return DownloadedImage(content=b"fixture-image", extension=".jpg")


def test_image_manager_keeps_successful_files_when_one_download_fails(tmp_path: Path) -> None:
    """Each image is independently recoverable and its saved path is persisted."""
    repository = SqliteRepository(tmp_path / "assistant.db")
    repository.initialize()
    task = repository.create_task("测试关键词")
    repository.save_products(
        task.id,
        [
            ProductCandidate(
                external_id="product-image-test",
                title="测试商品",
                price="¥10",
                description="",
                category="",
                url="https://www.goofish.com/item/test",
                image_urls=("https://img.example/ok.jpg", "https://img.example/broken.jpg"),
            )
        ],
    )
    product = repository.list_products(task.id)[0]

    summary = ImageManager(repository, tmp_path / "output", FakeImageFetcher()).download_product_images(
        product
    )

    updated_product = repository.list_products(task.id)[0]
    assert summary.downloaded == 1
    assert len(summary.errors) == 1
    assert updated_product.image_paths[0] is not None
    assert Path(updated_product.image_paths[0]).read_bytes() == b"fixture-image"
    assert updated_product.image_paths[1] is None

"""Resilient per-image downloads for collected product records."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from xianyu_assistant.domain.models import ProductRecord
from xianyu_assistant.persistence.sqlite_repository import SqliteRepository


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """Image bytes and a safe filename extension returned by a fetcher."""

    content: bytes
    extension: str


@dataclass(frozen=True, slots=True)
class ImageDownloadSummary:
    """Outcome of downloading all images associated with one product."""

    downloaded: int
    skipped: int
    errors: tuple[str, ...]


class ImageFetcher(Protocol):
    """Network boundary that keeps image download tests fully local."""

    def fetch(self, source_url: str) -> DownloadedImage:
        """Retrieve a single image or raise an exception with the failure reason."""


class UrllibImageFetcher:
    """Fetch images with a browser-like user agent and a bounded timeout."""

    _CONTENT_TYPE_EXTENSIONS: ClassVar[dict[str, str]] = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }

    def fetch(self, source_url: str) -> DownloadedImage:
        request = Request(source_url, headers={"User-Agent": "XianyuAssistant/0.1"})
        with urlopen(request, timeout=15) as response:
            content_type = response.headers.get_content_type().lower()
            if not content_type.startswith("image/"):
                raise ValueError(f"响应不是图片（Content-Type: {content_type}）。")
            content = response.read()
        if not content:
            raise ValueError("下载的图片为空。")
        return DownloadedImage(content=content, extension=self._extension(content_type, source_url))

    def _extension(self, content_type: str, source_url: str) -> str:
        if content_type in self._CONTENT_TYPE_EXTENSIONS:
            return self._CONTENT_TYPE_EXTENSIONS[content_type]
        url_suffix = Path(urlparse(source_url).path).suffix.lower()
        if url_suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return url_suffix
        guessed_extension = mimetypes.guess_extension(content_type)
        return guessed_extension if guessed_extension else ".jpg"


class ImageManager:
    """Write product images below the PRD's output/images directory structure."""

    def __init__(
        self,
        repository: SqliteRepository,
        output_directory: Path,
        fetcher: ImageFetcher | None = None,
    ) -> None:
        self._repository = repository
        self._output_directory = output_directory
        self._fetcher = fetcher or UrllibImageFetcher()

    def download_product_images(self, product: ProductRecord) -> ImageDownloadSummary:
        """Download missing images while retaining successful downloads on partial failure."""
        product_directory = self._output_directory / "images" / f"product{product.id:03d}"
        product_directory.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        skipped = 0
        errors: list[str] = []

        for position, source_url in enumerate(product.image_urls):
            current_path = product.image_paths[position] if position < len(product.image_paths) else None
            if current_path and Path(current_path).is_file():
                skipped += 1
                continue
            try:
                image = self._fetcher.fetch(source_url)
                target = product_directory / f"image{position + 1:03d}{image.extension}"
                temporary = target.with_suffix(f"{target.suffix}.part")
                temporary.write_bytes(image.content)
                temporary.replace(target)
                self._repository.update_image_path(product.id, position, str(target.resolve()))
                downloaded += 1
            except (OSError, ValueError) as error:
                errors.append(f"第 {position + 1} 张图片：{error}")
        return ImageDownloadSummary(downloaded=downloaded, skipped=skipped, errors=tuple(errors))

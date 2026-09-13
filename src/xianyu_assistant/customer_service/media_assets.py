"""Validation and hashing for user-registered local image resources."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from xianyu_assistant.customer_service.models import MediaAsset


class MediaAssetError(ValueError):
    """Raised when a local image cannot be safely registered."""


_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_MEDIA_BYTES = 10 * 1024 * 1024


def register_media_asset(
    path: Path,
    *,
    asset_id: str,
    product_key: str | None,
    display_name: str,
    scene_tag: str,
    description: str,
) -> MediaAsset:
    """Resolve, size-check, MIME-check, and hash one user-selected image."""
    try:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as error:
        raise MediaAssetError("图片文件不存在或无法读取。") from error
    mime_type, _ = mimetypes.guess_type(resolved.name)
    if mime_type not in _ALLOWED_MIME_TYPES:
        raise MediaAssetError("仅支持 JPG、PNG、WebP 或 GIF 图片。")
    if size <= 0 or size > _MAX_MEDIA_BYTES:
        raise MediaAssetError("图片大小必须大于 0 且不超过 10 MB。")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MediaAssetError("读取图片失败。") from error
    return MediaAsset(
        asset_id=asset_id,
        product_key=product_key,
        display_name=display_name,
        scene_tag=scene_tag,
        description=description,
        path=resolved,
        sha256=digest.hexdigest(),
        mime_type=mime_type,
    )

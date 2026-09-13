"""CS-2 tests for registered local image resources."""

from pathlib import Path

import pytest

from xianyu_assistant.customer_service.media_assets import MediaAssetError, register_media_asset


def test_media_asset_registration_resolves_path_and_records_sha256(tmp_path: Path) -> None:
    image_path = tmp_path / "detail.png"
    image_path.write_bytes(b"fake-png-content")

    asset = register_media_asset(
        image_path,
        asset_id="asset-1",
        product_key="product-1",
        display_name="细节图",
        scene_tag="规格说明",
        description="展示接口细节",
    )

    assert asset.path == image_path.resolve()
    assert asset.sha256
    assert asset.mime_type == "image/png"


def test_media_asset_registration_rejects_unsupported_files(tmp_path: Path) -> None:
    file_path = tmp_path / "secret.txt"
    file_path.write_text("not an image", encoding="utf-8")

    with pytest.raises(MediaAssetError):
        register_media_asset(
            file_path,
            asset_id="asset-1",
            product_key=None,
            display_name="文件",
            scene_tag="其他",
            description="",
        )

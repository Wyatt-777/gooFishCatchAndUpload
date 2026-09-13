"""CS-2 tests for product knowledge and registered media persistence."""

from pathlib import Path

from xianyu_assistant.customer_service.media_assets import register_media_asset
from xianyu_assistant.customer_service.models import ProductKnowledge
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def test_product_aliases_and_minimum_price_round_trip_with_normalized_matching(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_product_knowledge(
        ProductKnowledge(
            product_key="bike-1",
            name="公路车 轮组",
            aliases=("公路车轮组", "轮组"),
            platform_product_id="platform-1",
            listed_price="899",
            minimum_price="799",
            inventory_notes="现货",
        )
    )

    by_id = repository.find_product_knowledge(platform_product_id="platform-1")
    by_alias = repository.find_product_knowledge(normalized_title="轮组")

    assert by_id is not None
    assert by_id.minimum_price == "799"
    assert by_alias is not None
    assert by_alias.product_key == "bike-1"
    assert repository.list_product_knowledge()[0].aliases == ("公路车轮组", "轮组")


def test_media_asset_round_trip_preserves_registered_hash_and_path(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    image_path = tmp_path / "asset.jpg"
    image_path.write_bytes(b"test-image")
    asset = register_media_asset(
        image_path,
        asset_id="asset-1",
        product_key=None,
        display_name="通用图",
        scene_tag="规格",
        description="测试资源",
    )

    repository.save_media_asset(asset)
    saved = repository.list_media_assets()[0]

    assert saved.path == image_path.resolve()
    assert saved.sha256 == asset.sha256
    assert repository.get_media_asset("asset-1") == asset

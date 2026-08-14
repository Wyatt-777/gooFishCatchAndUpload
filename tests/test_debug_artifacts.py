"""Contracts for per-product collection diagnostics."""

import json

from xianyu_assistant.debug.artifacts import DebugArtifactWriter


def test_debug_writer_records_image_candidates_and_category_stages(tmp_path) -> None:
    writer = DebugArtifactWriter(tmp_path / "debug")

    writer.record_images(
        external_id="item-42",
        product_url="https://www.goofish.com/item?id=item-42",
        candidates=(
            {
                "source": "thumbnail",
                "url": "https://img.example/one-thumb.webp",
                "class": "gallery-image",
                "dom_path": "div.gallery > img.gallery-image",
            },
        ),
        selected_urls=("https://img.example/one-full.webp",),
    )
    writer.record_category_collection(
        external_id="item-42",
        product_url="https://www.goofish.com/item?id=item-42",
        raw_detail_category={"channelCatId": 300, "channelCatName": "锂电池"},
        category_path=("电动车配件", "电池", "锂电池"),
        category_ids=("100", "200", "300"),
    )
    writer.record_category_saved(
        product_id=7,
        external_id="item-42",
        category_path=("电动车配件", "电池", "锂电池"),
        category_ids=("100", "200", "300"),
    )
    writer.record_category_publish(
        product_id=7,
        category_input="锂电池",
        category_path=("电动车配件", "电池", "锂电池"),
        category_ids=("100", "200", "300"),
    )
    writer.record_publish_attributes(
        product_id=7,
        results=(
            {
                "source_name": "品牌",
                "source_value": "NARADA/南都",
                "status": "filled",
                "reason": "option_selected",
                "selected_value": "南都",
            },
        ),
    )

    image_payload = json.loads(
        (tmp_path / "debug" / "images" / "item-42.json").read_text(encoding="utf-8")
    )
    category_payload = json.loads(
        (tmp_path / "debug" / "category" / "item-42.json").read_text(encoding="utf-8")
    )
    publish_payload = json.loads(
        (tmp_path / "debug" / "category" / "product-7.json").read_text(encoding="utf-8")
    )
    attributes_payload = json.loads(
        (tmp_path / "debug" / "attributes" / "product-7.json").read_text(encoding="utf-8")
    )

    assert image_payload["candidate_images"][0]["dom_path"] == "div.gallery > img.gallery-image"
    assert image_payload["selected_image_urls"] == ["https://img.example/one-full.webp"]
    assert category_payload["parsed_category_path"] == ["电动车配件", "电池", "锂电池"]
    assert category_payload["saved_category_ids"] == ["100", "200", "300"]
    assert publish_payload["publish_category_input"] == "锂电池"
    assert attributes_payload["attribute_results"] == [
        {
            "source_name": "品牌",
            "source_value": "NARADA/南都",
            "status": "filled",
            "reason": "option_selected",
            "selected_value": "南都",
        }
    ]

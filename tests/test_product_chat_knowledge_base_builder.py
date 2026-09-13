"""Regression tests for the battery-only cleaned knowledge build."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_product_chat_knowledge_base import build


def test_build_keeps_only_battery_products_and_documents(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "statistics": {},
                "products": [
                    {"product_key": "battery", "name": "60V 锂电池"},
                    {"product_key": "course", "name": "经济师网课"},
                ],
                "conversations": [
                    {
                        "conversation_key": "battery-chat",
                        "product_key": "battery",
                        "messages": [
                            {
                                "message_key": "battery-message",
                                "direction": "incoming",
                                "message_type": "text",
                                "text": "这个60V电池能跑多远",
                            }
                        ],
                    },
                    {
                        "conversation_key": "course-chat",
                        "product_key": "course",
                        "messages": [
                            {
                                "message_key": "course-message",
                                "direction": "incoming",
                                "message_type": "text",
                                "text": "经济师网课怎么购买",
                            }
                        ],
                    },
                ],
                "historical_examples": [
                    {
                        "customer_text": "电池支持快充吗",
                        "merchant_text": "需要根据电芯和保护板确认",
                    },
                    {
                        "customer_text": "网课如何下载",
                        "merchant_text": "付款后发送资料",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload, documents, stats = build(source)

    assert [product["product_key"] for product in payload["products"]] == ["battery"]
    assert {document["product_category"] for document in documents} == {
        "电池与电动车配件"
    }
    assert stats["retained_product_count"] == 1
    assert stats["removed_non_battery_product_count"] == 1
    assert stats["cleaned_product_chat_count"] == 1
    assert stats["faq_candidate_count"] == 1

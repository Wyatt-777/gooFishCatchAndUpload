"""Tests for reliable adjacent-turn Q&A extraction."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_full_chat_qa_knowledge_base import build


def test_build_pairs_turns_keeps_short_answers_and_skips_unreliable(tmp_path: Path) -> None:
    source = tmp_path / "cleaned.json"
    source.write_text(
        json.dumps(
            {
                "knowledge_base_version": "xianyu_product_chat_kb_v1",
                "products": [],
                "documents": [
                    {
                        "document_type": "product_chat",
                        "product_key": "battery-1",
                        "source_conversation_keys": ["chat-1"],
                        "quality_flags": [],
                        "messages": [
                            {"message_key": "c1", "direction": "incoming", "text": "60V电池能买吗"},
                            {"message_key": "c2", "direction": "incoming", "text": "有货吗"},
                            {"message_key": "m1", "direction": "outgoing", "text": "可以"},
                            {"message_key": "c3", "direction": "incoming", "text": "包邮吗"},
                            {"message_key": "m2", "direction": "outgoing", "text": "包邮送到家"},
                        ],
                    },
                    {
                        "document_type": "product_chat",
                        "product_key": "battery-2",
                        "source_conversation_keys": ["chat-bad"],
                        "quality_flags": ["speaker_direction_may_be_unreliable"],
                        "messages": [
                            {"message_key": "x1", "direction": "incoming", "text": "电池多钱"},
                            {"message_key": "x2", "direction": "incoming", "text": "428元"},
                        ],
                    },
                    {
                        "document_type": "historical_qa_candidate",
                        "customer_text": "包邮吗",
                        "merchant_text": "包邮送到家",
                        "quality_flags": ["facts_require_verification"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload, documents, stats = build(source)

    assert payload["products"] == []
    assert stats["reliable_product_chat_documents"] == 1
    assert stats["skipped_direction_unreliable_documents"] == 1
    assert stats["extracted_adjacent_turns"] == 2
    assert stats["retained_unique_qa_count"] == 2
    first = next(item for item in documents if item["merchant_text"] == "可以")
    assert first["customer_text"] == "60V电池能买吗\n有货吗"
    assert first["product_key"] == "battery-1"
    assert first["source_conversation_keys"] == ["chat-1"]
    assert [message["message_key"] for message in first["messages"]] == ["c1", "c2", "m1"]
    duplicate = next(item for item in documents if item["customer_text"] == "包邮吗")
    assert "conversation_knowledge" in duplicate["quality_flags"]
    assert "historical_dynamic_fact" in duplicate["quality_flags"]


def test_build_can_add_battery_only_raw_history(tmp_path: Path) -> None:
    source = tmp_path / "cleaned.json"
    source.write_text(
        json.dumps(
            {
                "knowledge_base_version": "xianyu_product_chat_kb_v1",
                "products": [],
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps(
            {
                "historical_examples": [
                    {"customer_text": "锂电池能用吗", "merchant_text": "可以"},
                    {"customer_text": "网课能下载吗", "merchant_text": "可以"},
                    {"customer_text": "锂电池安全吗", "merchant_text": "不会爆炸"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _, documents, stats = build(source, raw)

    assert [(item["customer_text"], item["merchant_text"]) for item in documents] == [
        ("锂电池能用吗", "可以")
    ]
    assert stats["raw_historical_examples_scanned"] == 3
    assert stats["exclusion_counts"]["non_battery_raw_qa"] == 1
    assert stats["exclusion_counts"]["unusable_raw_qa"] == 1

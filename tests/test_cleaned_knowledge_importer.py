"""Tests for strict, idempotent cleaned-knowledge ingestion."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from xianyu_assistant.customer_service.knowledge_importer import (
    CleanedKnowledgeImporter,
    CleanedKnowledgeImportError,
)
from xianyu_assistant.customer_service.models import ProductKnowledge
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def _write_knowledge(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "knowledge_base_version": "xianyu_product_chat_kb_v1",
                "products": [
                    {
                        "product_key": "p1",
                        "platform_product_id": "item-1",
                        "name": "测试电池",
                        "aliases": ["电池别名"],
                        "listed_price": "99",
                        "minimum_price": None,
                        "specifications": "60V20Ah",
                        "inventory_notes": "",
                        "shipping_notes": "",
                        "after_sales_notes": "",
                        "enabled": True,
                    }
                ],
                "documents": [
                    {
                        "knowledge_id": "chat-1",
                        "document_type": "product_chat",
                        "product_key": "p1",
                        "product_category": "电池与电动车配件",
                        "chat_intents": ["规格与配置"],
                        "content": "incoming: 电话 13800138000，这个容量多大？",
                        "messages": [{"message_key": "m1"}],
                        "source_conversation_keys": ["c1"],
                        "quality_flags": ["facts_require_verification"],
                        "usage_note": "历史内容，需确认。",
                    },
                    {
                        "knowledge_id": "faq-1",
                        "document_type": "historical_qa_candidate",
                        "product_key": None,
                        "product_category": "电池与电动车配件",
                        "chat_intents": ["物流与发货"],
                        "customer_text": "什么时候发货？",
                        "merchant_text": "今天安排。",
                        "content": "顾客：什么时候发货？\n商家：今天安排。",
                        "source_conversation_keys": [],
                        "quality_flags": ["product_unlinked"],
                        "usage_note": "仅作口吻参考。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_cleaned_knowledge_is_validated_and_redacted(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.json"
    _write_knowledge(source)

    bundle = CleanedKnowledgeImporter().parse(source)

    assert bundle.preview.product_count == 1
    assert bundle.preview.chat_document_count == 1
    assert bundle.preview.example_count == 1
    assert "13800138000" not in bundle.documents[0].content
    assert bundle.documents[0].source_message_keys == ("m1",)
    assert bundle.examples[0].trust_level == "cleaned_history"


def test_cleaned_knowledge_rejects_an_unversioned_json_file(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.json"
    source.write_text('{"products": [], "documents": []}', encoding="utf-8")

    with pytest.raises(CleanedKnowledgeImportError, match="版本不受支持"):
        CleanedKnowledgeImporter().parse(source)


def test_explicit_user_curated_qa_is_imported_as_authoritative_knowledge(
    tmp_path: Path,
) -> None:
    source = tmp_path / "knowledge.json"
    _write_knowledge(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["documents"][1]["quality_flags"] = ["user_curated"]
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    bundle = CleanedKnowledgeImporter().parse(source)
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.store_cleaned_knowledge(bundle)

    assert bundle.examples[0].trust_level == "user_curated"
    answers = repository.find_curated_knowledge_answers("什么时候发货", 5)
    assert [answer.merchant_text for answer in answers] == ["今天安排。"]
    assert repository.find_historical_examples("什么时候发货", 5) == []


def test_conversation_knowledge_flag_gets_searchable_fact_trust(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.json"
    _write_knowledge(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["documents"][1]["quality_flags"] = ["conversation_knowledge"]
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    bundle = CleanedKnowledgeImporter().parse(source)
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.store_cleaned_knowledge(bundle)

    assert bundle.examples[0].trust_level == "conversation_knowledge"
    answers = repository.find_knowledge_answers("什么时候发货", 5)
    assert [answer.merchant_text for answer in answers] == ["今天安排。"]


def test_repository_merge_is_idempotent_and_preserves_human_facts(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.json"
    _write_knowledge(source)
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_product_knowledge(
        ProductKnowledge(
            product_key="p1",
            name="人工维护名称",
            aliases=("人工别名",),
            platform_product_id="item-1",
            listed_price="120",
            minimum_price="88",
            specifications="人工确认规格",
            enabled=False,
        )
    )
    bundle = CleanedKnowledgeImporter().parse(source)

    assert repository.store_cleaned_knowledge(bundle) is True
    assert repository.store_cleaned_knowledge(bundle) is False

    product = repository.list_product_knowledge()[0]
    assert product.name == "人工维护名称"
    assert product.listed_price == "120"
    assert product.minimum_price == "88"
    assert product.specifications == "人工确认规格"
    assert product.enabled is False
    assert set(product.aliases) == {"人工别名", "电池别名"}

    documents = repository.list_cleaned_knowledge_documents()
    assert len(documents) == 2
    assert documents[0].source_message_keys == ("m1",)
    assert repository.find_historical_examples("什么时候能够发货", 5)[0].example_id == "faq-1"
    with sqlite3.connect(tmp_path / "assistant.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM cs_knowledge_import_batches").fetchone()[0] == 1
        assert connection.execute(
            "SELECT trust_level FROM cs_training_examples WHERE example_id = 'faq-1'"
        ).fetchone()[0] == "cleaned_history"

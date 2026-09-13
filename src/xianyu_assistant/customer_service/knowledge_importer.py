"""Strict import of reviewed, cleaned customer-service knowledge JSON."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xianyu_assistant.customer_service.models import HistoricalExample, ProductKnowledge
from xianyu_assistant.customer_service.privacy import PrivacyRedactor


class CleanedKnowledgeImportError(ValueError):
    """Raised when a cleaned knowledge file is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class CleanedKnowledgeDocument:
    """One searchable document with source identifiers retained for audit."""

    knowledge_id: str
    document_type: str
    content: str
    product_key: str | None
    product_category: str
    chat_intents: tuple[str, ...]
    source_conversation_keys: tuple[str, ...]
    source_message_keys: tuple[str, ...]
    quality_flags: tuple[str, ...]
    usage_note: str


@dataclass(frozen=True, slots=True)
class CleanedKnowledgePreview:
    """Counts safe to show before an explicit database write."""

    source_name: str
    source_sha256: str
    product_count: int
    document_count: int
    chat_document_count: int
    example_count: int
    error_count: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanedKnowledgeBundle:
    """Validated records ready for one atomic, idempotent import."""

    source_name: str
    source_sha256: str
    products: tuple[ProductKnowledge, ...]
    documents: tuple[CleanedKnowledgeDocument, ...]
    examples: tuple[HistoricalExample, ...]
    errors: tuple[str, ...] = ()

    @property
    def preview(self) -> CleanedKnowledgePreview:
        return CleanedKnowledgePreview(
            source_name=self.source_name,
            source_sha256=self.source_sha256,
            product_count=len(self.products),
            document_count=len(self.documents),
            chat_document_count=sum(
                document.document_type == "product_chat" for document in self.documents
            ),
            example_count=len(self.examples),
            error_count=len(self.errors),
            errors=self.errors,
        )


class CleanedKnowledgeImporter:
    """Parse only the project's versioned cleaned-knowledge schema."""

    VERSION = "xianyu_product_chat_kb_v1"
    MAX_FILE_BYTES = 50 * 1024 * 1024
    MAX_PRODUCTS = 10_000
    MAX_DOCUMENTS = 50_000
    MAX_CONTENT_CHARACTERS = 20_000

    def preview(self, source_path: Path) -> CleanedKnowledgePreview:
        """Validate a file without mutating SQLite."""
        return self.parse(source_path).preview

    def parse(self, source_path: Path) -> CleanedKnowledgeBundle:
        """Return a redacted bundle or fail before any persistent write."""
        if not source_path.is_file():
            raise CleanedKnowledgeImportError(f"找不到清洗知识库文件：{source_path}")
        if source_path.suffix.casefold() != ".json":
            raise CleanedKnowledgeImportError("清洗知识库仅支持 JSON 文件。")
        if source_path.stat().st_size > self.MAX_FILE_BYTES:
            raise CleanedKnowledgeImportError("清洗知识库超过 50 MB 安全限制。")
        payload = source_path.read_bytes()
        try:
            data = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CleanedKnowledgeImportError("清洗知识库不是有效的 UTF-8 JSON。") from error
        if not isinstance(data, dict) or data.get("knowledge_base_version") != self.VERSION:
            raise CleanedKnowledgeImportError("清洗知识库版本不受支持。")
        raw_products = data.get("products")
        raw_documents = data.get("documents")
        if not isinstance(raw_products, list) or not isinstance(raw_documents, list):
            raise CleanedKnowledgeImportError("清洗知识库缺少 products 或 documents 数组。")
        if len(raw_products) > self.MAX_PRODUCTS or len(raw_documents) > self.MAX_DOCUMENTS:
            raise CleanedKnowledgeImportError("清洗知识库记录数量超过安全限制。")

        redactor = PrivacyRedactor()
        errors: list[str] = []
        products = self._parse_products(raw_products, redactor, errors)
        documents, examples = self._parse_documents(raw_documents, redactor, errors)
        return CleanedKnowledgeBundle(
            source_name=source_path.name,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            products=tuple(products),
            documents=tuple(documents),
            examples=tuple(examples),
            errors=tuple(errors),
        )

    def _parse_products(
        self,
        rows: list[object],
        redactor: PrivacyRedactor,
        errors: list[str],
    ) -> list[ProductKnowledge]:
        products: list[ProductKnowledge] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                errors.append(f"第 {index} 个商品不是对象。")
                continue
            product_key = _clean_string(row.get("product_key"))
            name = redactor.redact(_clean_string(row.get("name")))
            if not product_key or not name or product_key in seen:
                errors.append(f"第 {index} 个商品缺少唯一 product_key 或 name。")
                continue
            seen.add(product_key)
            aliases_value = row.get("aliases", [])
            aliases = (
                tuple(
                    dict.fromkeys(
                        redactor.redact(_clean_string(alias))
                        for alias in aliases_value
                        if _clean_string(alias)
                    )
                )
                if isinstance(aliases_value, list)
                else ()
            )
            products.append(
                ProductKnowledge(
                    product_key=product_key,
                    name=name,
                    aliases=aliases,
                    platform_product_id=_optional_string(row.get("platform_product_id")),
                    listed_price=_optional_string(row.get("listed_price")),
                    minimum_price=_optional_string(row.get("minimum_price")),
                    specifications=redactor.redact(_clean_string(row.get("specifications"))),
                    inventory_notes=redactor.redact(_clean_string(row.get("inventory_notes"))),
                    shipping_notes=redactor.redact(_clean_string(row.get("shipping_notes"))),
                    after_sales_notes=redactor.redact(_clean_string(row.get("after_sales_notes"))),
                    enabled=bool(row.get("enabled", True)),
                )
            )
        return products

    def _parse_documents(
        self,
        rows: list[object],
        redactor: PrivacyRedactor,
        errors: list[str],
    ) -> tuple[list[CleanedKnowledgeDocument], list[HistoricalExample]]:
        documents: list[CleanedKnowledgeDocument] = []
        examples: list[HistoricalExample] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                errors.append(f"第 {index} 个知识文档不是对象。")
                continue
            knowledge_id = _clean_string(row.get("knowledge_id"))
            document_type = _clean_string(row.get("document_type"))
            if (
                not knowledge_id
                or knowledge_id in seen
                or document_type not in {"product_chat", "historical_qa_candidate"}
            ):
                errors.append(f"第 {index} 个知识文档标识重复、缺失或类型不受支持。")
                continue
            content = redactor.redact(_clean_string(row.get("content")))
            if not content or len(content) > self.MAX_CONTENT_CHARACTERS:
                errors.append(f"知识文档 {knowledge_id} 内容为空或超过长度限制。")
                continue
            seen.add(knowledge_id)
            source_messages = row.get("messages", [])
            source_message_keys = tuple(
                dict.fromkeys(
                    _clean_string(message.get("message_key"))
                    for message in source_messages
                    if isinstance(message, dict) and _clean_string(message.get("message_key"))
                )
            )
            document = CleanedKnowledgeDocument(
                knowledge_id=knowledge_id,
                document_type=document_type,
                content=content,
                product_key=_optional_string(row.get("product_key")),
                product_category=_clean_string(row.get("product_category")) or "其他或未明确",
                chat_intents=_string_tuple(row.get("chat_intents")),
                source_conversation_keys=_string_tuple(row.get("source_conversation_keys")),
                source_message_keys=source_message_keys,
                quality_flags=_string_tuple(row.get("quality_flags")),
                usage_note=redactor.redact(_clean_string(row.get("usage_note"))),
            )
            documents.append(document)
            if document_type == "historical_qa_candidate":
                customer = redactor.redact(_clean_string(row.get("customer_text")))
                merchant = redactor.redact(_clean_string(row.get("merchant_text")))
                if not customer or not merchant:
                    errors.append(f"问答文档 {knowledge_id} 缺少顾客问题或商家回复。")
                    documents.pop()
                    seen.remove(knowledge_id)
                    continue
                examples.append(
                    HistoricalExample(
                        example_id=knowledge_id,
                        customer_text=customer,
                        merchant_text=merchant,
                        trust_level=_knowledge_trust_level(document.quality_flags),
                    )
                )
        return documents, examples


def _clean_string(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _optional_string(value: Any) -> str | None:
    cleaned = _clean_string(value)
    return cleaned or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(_clean_string(item) for item in value if _clean_string(item)))


def _knowledge_trust_level(quality_flags: tuple[str, ...]) -> str:
    if "user_curated" in quality_flags:
        return "user_curated"
    if "conversation_knowledge" in quality_flags:
        return "conversation_knowledge"
    return "cleaned_history"

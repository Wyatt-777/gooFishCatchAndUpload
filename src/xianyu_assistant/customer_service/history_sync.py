"""Read-only live chat synchronization into the local customer-service knowledge base."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from xianyu_assistant.customer_service.fingerprints import (
    content_fingerprint,
    normalize_for_matching,
)
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    HistoricalExample,
    MessageDirection,
    ProductKnowledge,
)
from xianyu_assistant.customer_service.privacy import PrivacyRedactor
from xianyu_assistant.customer_service.protocols import CustomerServiceRepository, XianyuChatAdapter


@dataclass(frozen=True, slots=True)
class LiveSyncResult:
    """Bounded outcome shown by the settings page after one synchronization."""

    conversation_count: int = 0
    message_count: int = 0
    product_count: int = 0
    example_count: int = 0
    errors: tuple[str, ...] = ()


class CustomerServiceHistorySynchronizer:
    """Turn the current account's rendered chats into redacted local knowledge."""

    def __init__(
        self,
        repository: CustomerServiceRepository,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._repository = repository
        self._progress = progress

    def sync(self, adapter: XianyuChatAdapter) -> LiveSyncResult:
        """Read all conversations, persist their messages, and merge product facts."""
        return self.sync_selected(adapter, adapter.list_all_conversations())

    def sync_selected(
        self,
        adapter: XianyuChatAdapter,
        summaries: list[ConversationSummary],
    ) -> LiveSyncResult:
        """Read a known batch of summaries, enabling resumable/batched backfills."""
        redactor = PrivacyRedactor()
        errors: list[str] = []
        message_count = 0
        product_keys: set[str] = set()
        example_count = 0
        for index, summary in enumerate(summaries, start=1):
            self._notify(f"正在整理第 {index}/{len(summaries)} 个会话…")
            try:
                adapter.open_conversation(summary.conversation_key)
                raw_snapshot = adapter.read_conversation()
                snapshot, display_name = _redact_snapshot(raw_snapshot, summary, redactor)
                examples = _historical_examples(snapshot)
                self._repository.store_live_snapshot(
                    snapshot,
                    display_name=display_name,
                    examples=examples,
                )
                message_count += len(snapshot.messages)
                example_count += len(examples)
                product = _product_from_chat(snapshot, summary, examples)
                if product is not None:
                    merged = self._save_product(product)
                    product_keys.add(merged.product_key)
            except Exception as error:  # noqa: BLE001 - one bad DOM row must not lose the batch
                errors.append(f"会话 {index} 整理失败：{_safe_error(error)}")
        self._notify("聊天记录整理完成。")
        return LiveSyncResult(
            conversation_count=len(summaries),
            message_count=message_count,
            product_count=len(product_keys),
            example_count=example_count,
            errors=tuple(errors),
        )

    def sync_summaries(self, adapter: XianyuChatAdapter) -> LiveSyncResult:
        """Persist the complete session index and product metadata without opening chats."""
        summaries = adapter.list_all_conversations()
        redactor = PrivacyRedactor()
        product_keys: set[str] = set()
        for index, summary in enumerate(summaries, start=1):
            self._notify(f"正在登记第 {index}/{len(summaries)} 个会话的商品信息…")
            safe_summary = replace(
                summary,
                display_name=redactor.redact(summary.display_name).strip()
                if summary.display_name
                else None,
                product_title=redactor.redact(summary.product_title).strip()
                if summary.product_title
                else None,
                last_message_text=redactor.redact(summary.last_message_text).strip()
                if summary.last_message_text
                else None,
            )
            self._repository.store_live_summary(safe_summary)
            product = _product_from_chat(
                ConversationSnapshot(
                    conversation_key=safe_summary.conversation_key,
                    messages=(),
                    platform_product_id=safe_summary.platform_product_id,
                    product_title=safe_summary.product_title,
                ),
                safe_summary,
                (),
            )
            if product is not None:
                product_keys.add(self._save_product(product).product_key)
        self._notify("商品索引登记完成。")
        return LiveSyncResult(
            conversation_count=len(summaries),
            product_count=len(product_keys),
        )

    def _save_product(self, product: ProductKnowledge) -> ProductKnowledge:
        existing = self._repository.find_product_knowledge(
            platform_product_id=product.platform_product_id,
            normalized_title=normalize_for_matching(product.name),
        )
        merged = _merge_product(existing, product)
        self._repository.save_product_knowledge(merged)
        return merged

    def _notify(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)


def _redact_snapshot(
    snapshot: ConversationSnapshot,
    summary: ConversationSummary,
    redactor: PrivacyRedactor,
) -> tuple[ConversationSnapshot, str | None]:
    messages: list[ChatMessage] = []
    for message in snapshot.messages:
        text = redactor.redact(message.text or "")
        messages.append(replace(message, text=text))
    title = snapshot.product_title or summary.product_title
    redacted_title = redactor.redact(title).strip() if title else None
    display_name = redactor.redact(summary.display_name).strip() if summary.display_name else None
    return replace(snapshot, messages=tuple(messages), product_title=redacted_title), display_name


def _historical_examples(snapshot: ConversationSnapshot) -> tuple[HistoricalExample, ...]:
    pending_customer: list[str] = []
    examples: list[HistoricalExample] = []
    for message in snapshot.messages:
        text = (message.text or "").strip()
        if not text:
            continue
        if message.direction is MessageDirection.INCOMING:
            pending_customer.append(text)
            continue
        if message.direction is MessageDirection.OUTGOING and pending_customer:
            customer_text = "\n".join(pending_customer)
            example_id = "live-" + content_fingerprint(snapshot.conversation_key, customer_text, text)[:32]
            examples.append(
                HistoricalExample(
                    example_id=example_id,
                    customer_text=customer_text,
                    merchant_text=text,
                    trust_level="imported_history",
                )
            )
            pending_customer = []
    return tuple(examples)


def _product_from_chat(
    snapshot: ConversationSnapshot,
    summary: ConversationSummary,
    examples: tuple[HistoricalExample, ...],
) -> ProductKnowledge | None:
    product_id = snapshot.platform_product_id or summary.platform_product_id
    name = (snapshot.product_title or summary.product_title or "").strip()
    if not _usable_product_title(name):
        name = ""
    if not product_id and not name:
        return None
    if not name:
        name = f"闲鱼商品 {product_id}"
    product_key = f"xianyu-{product_id}" if product_id else "chat-" + hashlib.sha256(
        normalize_for_matching(name).encode("utf-8")
    ).hexdigest()[:16]
    lines = [
        "[自动整理自当前账号历史聊天，仅供人工校验，不替代最新确认]",
        f"关联会话：{snapshot.conversation_key}",
        f"本次读取消息：{len(snapshot.messages)} 条",
    ]
    if examples:
        lines.append("历史问答参考：")
        for example in examples[:8]:
            lines.append(f"顾客：{example.customer_text}\n商家：{example.merchant_text}")
    else:
        lines.append("本会话暂未形成完整的顾客-商家问答轮次。")
    if summary.last_message_text:
        lines.append(f"最近一条会话摘要：{summary.last_message_text}")
    return ProductKnowledge(
        product_key=product_key,
        name=name,
        aliases=(name,),
        platform_product_id=product_id,
        listed_price=summary.listed_price,
        supplementary_knowledge="\n".join(lines)[:8_000],
    )


def _merge_product(existing: ProductKnowledge | None, inferred: ProductKnowledge) -> ProductKnowledge:
    if existing is None:
        return inferred
    existing_note = existing.supplementary_knowledge.strip()
    inferred_note = inferred.supplementary_knowledge.strip()
    if inferred_note and inferred_note not in existing_note:
        combined_note = (existing_note + "\n\n" + inferred_note).strip()
    else:
        combined_note = existing_note
    return ProductKnowledge(
        product_key=existing.product_key,
        name=(
            inferred.name
            if _usable_product_title(inferred.name) and not _usable_product_title(existing.name)
            else existing.name
        )
        or inferred.name,
        aliases=tuple(dict.fromkeys((*existing.aliases, *inferred.aliases))),
        platform_product_id=existing.platform_product_id or inferred.platform_product_id,
        listed_price=existing.listed_price or inferred.listed_price,
        minimum_price=existing.minimum_price,
        specifications=existing.specifications,
        inventory_notes=existing.inventory_notes,
        shipping_notes=existing.shipping_notes,
        after_sales_notes=existing.after_sales_notes,
        supplementary_knowledge=combined_note[:8_000],
        enabled=existing.enabled,
    )


def _safe_error(error: Exception) -> str:
    message = str(error).strip()
    return message[:180] or error.__class__.__name__


_INVALID_PRODUCT_TITLE_RE = re.compile(
    r"(?:交易(?:成功|关闭)|查看钱款|含运费|快给ta一个评价|^¥?\d+(?:\.\d+)?$|通知消息)"
)


def _usable_product_title(title: str) -> bool:
    """Reject order/status text that the chat page sometimes exposes as itemInfo.title."""
    normalized = " ".join(title.split())
    return (
        bool(normalized)
        and not normalized.startswith("闲鱼商品 ")
        and not _INVALID_PRODUCT_TITLE_RE.search(normalized)
    )

"""Read-only live chat synchronization tests."""

import sqlite3
from pathlib import Path

from xianyu_assistant.customer_service.history_sync import CustomerServiceHistorySynchronizer
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


class FakeLiveAdapter:
    def __init__(self) -> None:
        self.summary = ConversationSummary(
            "session-1",
            display_name="顾客",
            platform_product_id="item-1",
            product_title="测试商品",
            listed_price="99",
        )

    def list_all_conversations(self) -> list[ConversationSummary]:
        return [self.summary]

    def open_conversation(self, conversation_key: str) -> None:
        assert conversation_key == "session-1"

    def read_conversation(self) -> ConversationSnapshot:
        return ConversationSnapshot(
            "session-1",
            (
                ChatMessage("m1", MessageDirection.INCOMING, MessageKind.TEXT, "请问电话13800138000"),
                ChatMessage("m2", MessageDirection.OUTGOING, MessageKind.TEXT, "99元包邮"),
            ),
            platform_product_id="item-1",
            product_title="测试商品",
        )


def test_live_sync_redacts_messages_and_merges_product_knowledge(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()

    result = CustomerServiceHistorySynchronizer(repository).sync(FakeLiveAdapter())

    assert result.conversation_count == 1
    assert result.message_count == 2
    assert result.product_count == 1
    assert result.example_count == 1
    product = repository.find_product_knowledge(platform_product_id="item-1")
    assert product is not None
    assert product.name == "测试商品"
    with sqlite3.connect(tmp_path / "assistant.db") as connection:
        texts = [row[0] for row in connection.execute("SELECT text FROM cs_messages")]
        assert all("13800138000" not in text for text in texts)
        assert connection.execute("SELECT COUNT(*) FROM cs_training_examples").fetchone()[0] == 1

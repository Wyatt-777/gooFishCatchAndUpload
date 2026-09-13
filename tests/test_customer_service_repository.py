"""CS-1 tests for the additive customer-service SQLite repository."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xianyu_assistant.customer_service.importer import ChatHistoryImporter
from xianyu_assistant.customer_service.models import (
    PriceChangeDraft,
    PriceChangeStatus,
    ReplyDraft,
    ReplyJobStatus,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def test_import_is_idempotent_and_persists_only_redacted_content(tmp_path: Path) -> None:
    source_path = tmp_path / "history.json"
    source_path.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "conversation_key": "c1",
                        "messages": [
                            {
                                "message_key": "m1",
                                "direction": "incoming",
                                "text": "电话 13800138000",
                            },
                            {"message_key": "m2", "direction": "outgoing", "text": "好的"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    bundle = ChatHistoryImporter().parse(source_path)

    assert repository.store_import(bundle) is True
    assert repository.store_import(bundle) is False
    assert len(repository.list_historical_examples()) == 1

    with sqlite3.connect(database_path) as connection:
        texts = [row[0] for row in connection.execute("SELECT text FROM cs_messages")]
        assert all("13800138000" not in text for text in texts)
        assert connection.execute("SELECT COUNT(*) FROM cs_import_batches").fetchone()[0] == 1


def test_audit_cleanup_removes_expired_content_but_keeps_knowledge_inputs(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    repository.add_processed_fingerprint("permanent-fingerprint", datetime.now(UTC))
    repository.save_reply_draft(
        ReplyDraft(
            job_id="job-1",
            conversation_key="old-conversation",
            batch_fingerprint="old-batch",
            reply_text="旧草稿",
            status=ReplyJobStatus.AWAITING_REVIEW,
        )
    )

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            INSERT INTO cs_conversations
                (conversation_key, display_name, platform_product_id, updated_at, content_expires_at)
            VALUES ('old-conversation', '旧顾客', NULL, '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00');
            INSERT INTO cs_messages
                (conversation_key, message_key, direction, message_type, text, created_at)
            VALUES ('old-conversation', 'old-message', 'incoming', 'text', '旧消息', '2025-01-01T00:00:00+00:00');
            INSERT INTO cs_handoff_events
                (conversation_key, reason, status, created_at, content_expires_at)
            VALUES ('old-conversation', '旧原因', 'open', '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00');
            INSERT INTO cs_run_sessions
                (mode, status, started_at, content_expires_at)
            VALUES ('human_confirmation', 'stopped', '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00');
            """
        )
        connection.commit()

    deleted = repository.cleanup_expired_audit(
        now=datetime(2025, 2, 1, tzinfo=UTC),
        retention=timedelta(days=15),
    )

    assert deleted >= 4
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cs_processed_fingerprints").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cs_messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_handoff_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_run_sessions").fetchone()[0] == 0


def test_repository_detects_only_an_earlier_reply_job(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_reply_draft(
        ReplyDraft(
            job_id="job-1",
            conversation_key="conversation-1",
            batch_fingerprint="batch-1",
            reply_text="第一条",
            status=ReplyJobStatus.AWAITING_REVIEW,
        )
    )

    assert not repository.has_prior_reply_job("conversation-1", exclude_job_id="job-1")
    assert repository.has_prior_reply_job("conversation-1", exclude_job_id="job-2")
    assert not repository.has_prior_reply_job("conversation-2", exclude_job_id="job-2")


def test_explicit_price_change_record_delete_is_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_price_change_draft(
        PriceChangeDraft(
            task_id="failed-price-change",
            conversation_key="conversation-1",
            customer_message_key="message-1",
            product_key="battery-6030",
            product_name="60V30Ah 原装铁塔",
            customer_offer="650.00",
            proposed_price="650.00",
            minimum_price="650.00",
            listed_price="678.00",
            status=PriceChangeStatus.FAILED,
            failure_reason="页面改价结果无法确认",
        )
    )

    assert repository.delete_price_change_draft("failed-price-change") is True
    assert repository.list_price_change_drafts() == []
    assert repository.delete_price_change_draft("failed-price-change") is False


def test_price_change_round_trip_preserves_optional_price_adjustment(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    draft = PriceChangeDraft(
        task_id="bluetooth-price-change",
        conversation_key="conversation-1",
        customer_message_key="message-1",
        product_key="current-tieta-60v20ah",
        product_name="原装25年铁塔 60V20Ah（6020） + 蓝牙模块",
        customer_offer="400.00",
        proposed_price="400.00",
        minimum_price="398.00",
        listed_price="418.00",
        rationale="价格包含20元蓝牙选装费用。",
        price_adjustment_key="bluetooth_module",
        price_adjustment_amount="20.00",
    )

    repository.save_price_change_draft(draft)

    assert repository.list_price_change_drafts() == [draft]


def test_handoff_notification_is_deduplicated_and_can_release_conversation(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    created_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    repository.save_handoff_event(
        conversation_key="conversation-1",
        reason="涉及售后争议",
        created_at=created_at,
    )
    repository.save_handoff_event(
        conversation_key="conversation-1",
        reason="重复触发不应重复通知",
        created_at=created_at,
    )

    assert repository.has_open_handoff("conversation-1") is True
    events = repository.list_open_handoff_events()
    assert len(events) == 1
    assert events[0].reason == "涉及售后争议"

    assert repository.resolve_handoff_event(events[0].event_id) is True
    assert repository.resolve_handoff_event(events[0].event_id) is False
    assert repository.has_open_handoff("conversation-1") is False
    assert repository.list_open_handoff_events() == []

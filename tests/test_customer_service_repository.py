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
    SalesStage,
    SalesState,
)
from xianyu_assistant.customer_service.negotiation import NegotiationState
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
    repository.save_negotiation_state(
        "old-conversation",
        NegotiationState(
            product_key="current-tieta-60v20ah",
            last_customer_offer="378",
            accepted_price="378",
            outcome="accept",
        ),
        price_variant="base",
    )
    repository.save_sales_state(
        SalesState(
            conversation_key="old-conversation",
            product_key="current-tieta-60v20ah",
            stage=SalesStage.QUALIFY,
            follow_up_text="还需要我帮你配吗",
            follow_up_due_at=datetime(2025, 1, 1, tzinfo=UTC),
            last_merchant_fingerprint="old-sales-reply",
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
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
            UPDATE cs_negotiation_states
            SET updated_at = '2025-01-01T00:00:00+00:00'
            WHERE conversation_key = 'old-conversation';
            UPDATE cs_sales_states
            SET updated_at = '2025-01-01T00:00:00+00:00'
            WHERE conversation_key = 'old-conversation';
            """
        )
        connection.commit()

    deleted = repository.cleanup_expired_audit(
        now=datetime(2025, 2, 1, tzinfo=UTC),
        retention=timedelta(days=15),
    )

    assert deleted >= 5
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cs_processed_fingerprints").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cs_messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_handoff_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_run_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_negotiation_states").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_sales_states").fetchone()[0] == 0


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


def test_negotiation_state_round_trip_survives_repository_recreation(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    state = NegotiationState(
        product_key="current-tieta-60v20ah",
        quantity=1,
        last_customer_offer="378",
        accepted_price="378",
        outcome="accept",
    )

    repository.save_negotiation_state("conversation-1", state, price_variant="base")
    reopened = CustomerServiceRepository(database_path)

    assert reopened.load_negotiation_state("conversation-1") == (state, "base")


def test_sales_follow_up_round_trip_is_due_once_and_can_be_cancelled(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    due_at = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    state = SalesState(
        conversation_key="conversation-1",
        product_key="current-tieta-60v20ah",
        stage=SalesStage.QUALIFY,
        follow_up_text="方便说下车型和想跑的公里数",
        follow_up_due_at=due_at,
        last_customer_message_key="message-1",
        last_merchant_fingerprint="reply-fingerprint",
        updated_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )

    repository.save_sales_state(state)

    assert repository.list_due_sales_follow_ups(
        now=due_at - timedelta(seconds=1), limit=10
    ) == []
    assert repository.list_due_sales_follow_ups(now=due_at, limit=10) == [state]

    repository.cancel_sales_follow_up(
        "conversation-1", updated_at=due_at + timedelta(seconds=1)
    )
    assert repository.list_due_sales_follow_ups(
        now=due_at + timedelta(hours=1), limit=10
    ) == []


def test_sales_follow_up_marked_sent_cannot_be_selected_again(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    due_at = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    repository.save_sales_state(
        SalesState(
            conversation_key="conversation-1",
            product_key=None,
            stage=SalesStage.RECOMMEND,
            follow_up_text="你是二轮还是三轮",
            follow_up_due_at=due_at,
            last_merchant_fingerprint="first-reply",
            updated_at=due_at - timedelta(minutes=30),
        )
    )

    repository.mark_sales_follow_up_sent(
        "conversation-1",
        merchant_fingerprint="follow-up-reply",
        updated_at=due_at,
    )

    assert repository.list_due_sales_follow_ups(
        now=due_at + timedelta(days=1), limit=10
    ) == []
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT follow_up_count FROM cs_sales_states WHERE conversation_key = ?",
            ("conversation-1",),
        ).fetchone()[0] == 1


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


def test_customer_turn_version_and_send_reservation_are_atomic(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    version = repository.observe_customer_turn(
        turn_id="turn-1",
        conversation_key="conversation-1",
        message_keys=("message-1", "message-2"),
        customer_text="2000W\n带得动不",
        platform_product_id="product-1",
        observed_at=now,
    )
    assert version == 1
    assert repository.observe_customer_turn(
        turn_id="turn-1",
        conversation_key="conversation-1",
        message_keys=("message-1", "message-2"),
        customer_text="2000W\n带得动不",
        platform_product_id="product-1",
        observed_at=now,
    ) == 1

    assert repository.reserve_send(
        send_id="send-1",
        turn_id="turn-1",
        conversation_key="conversation-1",
        expected_version=1,
        expected_last_message_key="message-2",
        expected_product_id="product-1",
        reply_fingerprint="reply-fingerprint",
        created_at=now,
    ) is True

    assert repository.observe_customer_turn(
        turn_id="turn-2",
        conversation_key="conversation-1",
        message_keys=("message-3",),
        customer_text="补充一下",
        platform_product_id="product-1",
        observed_at=now,
    ) == 2
    assert repository.reserve_send(
        send_id="stale-send",
        turn_id="turn-1",
        conversation_key="conversation-1",
        expected_version=1,
        expected_last_message_key="message-2",
        expected_product_id="product-1",
        reply_fingerprint="reply-fingerprint",
        created_at=now,
    ) is False


def test_verified_send_persists_replayable_reply_and_handled_cursor(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    repository.observe_customer_turn(
        turn_id="turn-1",
        conversation_key="conversation-1",
        message_keys=("message-1",),
        customer_text="多少钱",
        platform_product_id="product-1",
        observed_at=now,
    )
    repository.update_customer_turn(
        "turn-1",
        status="drafted",
        semantic_json='{"primary_intent":"price_inquiry"}',
        decision_json='{"intent":"price_inquiry"}',
        reply_text="398元",
        updated_at=now,
    )
    assert repository.reserve_send(
        send_id="send-1",
        turn_id="turn-1",
        conversation_key="conversation-1",
        expected_version=1,
        expected_last_message_key="message-1",
        expected_product_id="product-1",
        reply_fingerprint="reply-fingerprint",
        created_at=now,
    )
    repository.mark_send_verified(
        "send-1",
        outgoing_message_key="outgoing-1",
        handled_message_key="message-1",
        updated_at=now,
    )

    with sqlite3.connect(repository.database_path) as connection:
        turn = connection.execute(
            "SELECT status, reply_text FROM cs_customer_turns WHERE turn_id = 'turn-1'"
        ).fetchone()
        state = connection.execute(
            "SELECT last_handled_message_key FROM cs_conversation_states "
            "WHERE conversation_key = 'conversation-1'"
        ).fetchone()
        outbox = connection.execute(
            "SELECT status, outgoing_message_key FROM cs_send_outbox WHERE send_id = 'send-1'"
        ).fetchone()
    assert turn == ("sent", "398元")
    assert state == ("message-1",)
    assert outbox == ("sent_verified", "outgoing-1")


def test_first_contact_catalog_reservation_is_persistent_and_at_most_once(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=False
    )
    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=True
    ) is False
    assert repository.reserve_first_contact_catalog(
        "conversation-1",
        reservation_id="job-1",
        reply_fingerprint="welcome-and-reply",
        created_at=now,
    )
    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=False
    ) is False
    assert repository.reserve_first_contact_catalog(
        "conversation-1",
        reservation_id="job-2",
        reply_fingerprint="welcome-and-reply",
        created_at=now,
    ) is False

    repository.release_first_contact_catalog_reservation(
        "conversation-1", reservation_id="job-1"
    )
    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=False
    )
    assert repository.reserve_first_contact_catalog(
        "conversation-1",
        reservation_id="job-3",
        reply_fingerprint="welcome-and-reply",
        created_at=now,
    )
    repository.mark_first_contact_catalog_sent(
        "conversation-1", reservation_id="job-3", updated_at=now
    )

    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=False
    ) is False
    repository.initialize()
    assert repository.should_send_first_contact_catalog(
        "conversation-1", snapshot_has_outgoing=False
    ) is False

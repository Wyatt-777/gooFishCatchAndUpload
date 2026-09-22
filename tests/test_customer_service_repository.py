"""CS-1 tests for the additive customer-service SQLite repository."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xianyu_assistant.customer_service.importer import ChatHistoryImporter
from xianyu_assistant.customer_service.models import (
    ConversationSummary,
    CustomerFulfillmentStatus,
    CustomerIntentLevel,
    CustomerLifecycle,
    CustomerOrderStatus,
    PriceChangeDraft,
    PriceChangeStatus,
    ReplyDraft,
    ReplyJobStatus,
    SalesStage,
    SalesState,
)
from xianyu_assistant.customer_service.negotiation import NegotiationState
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def test_customer_profiles_link_only_by_stable_platform_identity(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    observed_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)

    first = repository.observe_customer_summary(
        ConversationSummary("conversation-1", display_name="同名顾客"),
        observed_at=observed_at,
        platform_customer_id="buyer-1",
    )
    second = repository.observe_customer_summary(
        ConversationSummary("conversation-2", display_name="买家新昵称"),
        observed_at=observed_at + timedelta(minutes=1),
        platform_customer_id="buyer-1",
    )
    same_name_without_id = repository.observe_customer_summary(
        ConversationSummary("conversation-3", display_name="同名顾客"),
        observed_at=observed_at + timedelta(minutes=2),
    )

    assert first.customer_key == second.customer_key == "customer-buyer-1"
    assert second.conversation_count == 2
    assert second.display_name == "买家新昵称"
    assert same_name_without_id.customer_key == "conversation-3"
    assert len(repository.list_customer_profiles()) == 2


def test_delivered_order_lock_is_conversation_scoped_sticky_and_explicitly_restorable(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    observed_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    repository.observe_customer_summary(
        ConversationSummary(
            "conversation-1",
            display_name="顾客甲",
            platform_product_id="item-1",
        ),
        observed_at=observed_at,
    )

    state = repository.record_delivered_conversation(
        conversation_key="conversation-1",
        platform_product_id="item-1",
        evidence_key="platform-delivery-1",
        evidence_text="订单已签收",
        observed_at=observed_at,
    )
    repository.save_post_delivery_handoff(
        conversation_key="conversation-1",
        reason="平台显示订单已签收，顾客新消息已停止自动回复，请人工处理。",
        created_at=observed_at,
    )
    event = repository.list_open_handoff_events()[0]

    assert state.status is CustomerFulfillmentStatus.DELIVERED
    assert state.automation_status == "human_owned"
    assert event.handoff_type == "post_delivery"
    assert event.release_on_resolve is False
    assert repository.resolve_handoff_event(event.event_id) is True
    assert repository.is_post_delivery_human_owned("conversation-1") is True

    assert repository.restore_conversation_automation(
        "conversation-1",
        restored_at=observed_at + timedelta(minutes=1),
    )
    assert repository.is_post_delivery_human_owned("conversation-1") is False

    repeated = repository.record_delivered_conversation(
        conversation_key="conversation-1",
        platform_product_id="item-1",
        evidence_key="platform-delivery-1",
        evidence_text="订单已签收",
        observed_at=observed_at + timedelta(minutes=2),
    )
    assert repeated.automation_status == "active"
    profile = repository.find_customer_profile_by_conversation("conversation-1")
    assert profile is not None
    assert profile.fulfillment_status is CustomerFulfillmentStatus.DELIVERED
    assert profile.delivered_at == observed_at


def test_shipped_order_lock_is_durable_and_restorable(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    observed_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    repository.observe_customer_summary(
        ConversationSummary(
            "conversation-shipped",
            display_name="顾客乙",
            platform_product_id="item-2",
        ),
        observed_at=observed_at,
    )

    state = repository.record_shipped_conversation(
        conversation_key="conversation-shipped",
        platform_product_id="item-2",
        evidence_key="platform-shipment-1",
        evidence_text="你已发货",
        observed_at=observed_at,
    )

    assert state.status is CustomerFulfillmentStatus.SHIPPED
    assert state.shipped_at == observed_at
    assert state.delivered_at is None
    assert repository.is_post_delivery_human_owned("conversation-shipped") is True
    profile = repository.find_customer_profile_by_conversation("conversation-shipped")
    assert profile is not None
    assert profile.fulfillment_status is CustomerFulfillmentStatus.SHIPPED
    assert profile.shipped_at == observed_at

    assert repository.restore_conversation_automation(
        "conversation-shipped",
        restored_at=observed_at + timedelta(minutes=1),
    )
    repeated = repository.record_shipped_conversation(
        conversation_key="conversation-shipped",
        platform_product_id="item-2",
        evidence_key="platform-shipment-1",
        evidence_text="你已发货",
        observed_at=observed_at + timedelta(minutes=2),
    )
    assert repeated.automation_status == "active"


def test_customer_state_is_durable_idempotent_and_kept_after_audit_cleanup(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    observed_at = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    repository.observe_customer_summary(
        ConversationSummary(
            "conversation-1",
            display_name="顾客甲",
            platform_product_id="item-1",
        ),
        observed_at=observed_at,
    )

    qualified = repository.record_customer_state(
        event_key="turn-1:classified",
        conversation_key="conversation-1",
        event_type="customer_turn_classified",
        lifecycle=CustomerLifecycle.QUALIFIED,
        intent_level=CustomerIntentLevel.MEDIUM,
        observed_at=observed_at,
        product_key="current-tieta-60v30ah",
        sales_stage=SalesStage.RECOMMEND,
        order_status=CustomerOrderStatus.CONSIDERING,
        battery_model="60V30Ah",
        required_range_km="50",
        quantity=1,
        payload={"primary_intent": "range_inquiry"},
    )
    duplicate = repository.record_customer_state(
        event_key="turn-1:classified",
        conversation_key="conversation-1",
        event_type="customer_turn_classified",
        lifecycle=CustomerLifecycle.ENGAGED,
        intent_level=CustomerIntentLevel.LOW,
        observed_at=observed_at + timedelta(seconds=1),
    )
    ready = repository.record_customer_state(
        event_key="turn-2:classified",
        conversation_key="conversation-1",
        event_type="customer_turn_classified",
        lifecycle=CustomerLifecycle.READY_TO_ORDER,
        intent_level=CustomerIntentLevel.HIGH,
        observed_at=observed_at + timedelta(minutes=1),
        order_status=CustomerOrderStatus.PENDING_PAYMENT,
        last_customer_offer="650",
        accepted_price="650",
    )
    follow_up_at = observed_at + timedelta(minutes=31)
    repository.mark_customer_reply_sent(
        "conversation-1",
        event_key="turn-2:merchant-reply",
        sales_stage=SalesStage.CLOSE,
        next_follow_up_at=follow_up_at,
        sent_at=observed_at + timedelta(minutes=1),
    )

    assert qualified.battery_model == "60V30Ah"
    assert duplicate.lifecycle is CustomerLifecycle.QUALIFIED
    assert ready.lifecycle is CustomerLifecycle.READY_TO_ORDER
    reopened = CustomerServiceRepository(repository.database_path)
    profile = reopened.find_customer_profile_by_conversation("conversation-1")
    assert profile is not None
    assert profile.intent_level is CustomerIntentLevel.HIGH
    assert profile.accepted_price == "650"
    assert profile.sales_stage is SalesStage.CLOSE
    assert profile.next_follow_up_at == follow_up_at
    assert len(reopened.list_customer_state_events(profile.customer_key)) == 3

    repository.save_sales_state(
        SalesState(
            conversation_key="conversation-1",
            product_key="current-tieta-60v30ah",
            stage=SalesStage.CLOSE,
            follow_up_text="还需要我帮你改价吗",
            follow_up_due_at=follow_up_at,
            updated_at=observed_at + timedelta(minutes=1),
        )
    )
    repository.cancel_sales_follow_up(
        "conversation-1",
        updated_at=observed_at + timedelta(minutes=2),
    )
    cancelled = repository.get_customer_profile(profile.customer_key)
    assert cancelled is not None
    assert cancelled.next_follow_up_at is None

    repository.cleanup_expired_audit(
        now=observed_at + timedelta(days=31),
        retention=timedelta(days=15),
    )
    assert reopened.get_customer_profile(profile.customer_key) is not None
    assert len(reopened.list_customer_state_events(profile.customer_key)) == 3


def test_initialize_backfills_existing_conversations_into_customer_profiles(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO cs_conversations (
                conversation_key, display_name, platform_product_id,
                updated_at, content_expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-conversation",
                "历史顾客",
                "item-legacy",
                "2026-09-01T00:00:00+00:00",
                "2026-09-16T00:00:00+00:00",
            ),
        )
    repository.initialize()

    profile = repository.find_customer_profile_by_conversation("legacy-conversation")
    assert profile is not None
    assert profile.display_name == "历史顾客"
    assert profile.platform_product_id == "item-legacy"


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


def test_interrupted_jobs_are_recovered_without_blind_resend(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    for status, suffix in (
        (ReplyJobStatus.READING_CONTEXT, "read"),
        (ReplyJobStatus.SENDING, "send"),
    ):
        repository.save_reply_draft(
            ReplyDraft(
                job_id=f"job-{suffix}",
                conversation_key=f"conversation-{suffix}",
                batch_fingerprint=f"batch-{suffix}",
                reply_text="测试",
                status=status,
            )
        )

    recovered = repository.recover_interrupted_reply_jobs(
        recovered_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    )

    assert recovered == 2
    reading = repository.find_reply_draft(
        conversation_key="conversation-read", batch_fingerprint="batch-read"
    )
    sending = repository.find_reply_draft(
        conversation_key="conversation-send", batch_fingerprint="batch-send"
    )
    assert reading is not None and reading.status is ReplyJobStatus.FAILED
    assert sending is not None and sending.status is ReplyJobStatus.HANDOFF
    events = repository.list_open_handoff_events()
    assert [event.conversation_key for event in events] == ["conversation-send"]


def test_run_session_persists_specific_halt_reason(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.db"
    repository = CustomerServiceRepository(database_path)
    repository.initialize()
    started_at = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    run_id = repository.start_run_session(
        mode="auto_send",
        started_at=started_at,
    )
    repository.finish_run_session(
        run_id,
        status="halted",
        stopped_at=started_at + timedelta(minutes=1),
        stop_reason="闲鱼登录已失效，客服已暂停。",
        failure_category="page_health",
        failure_stage="customer_service_thread",
        exception_type="LoginRequired",
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT status, stop_reason, failure_category,
                   failure_stage, exception_type
            FROM cs_run_sessions WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    assert row == (
        "halted",
        "闲鱼登录已失效，客服已暂停。",
        "page_health",
        "customer_service_thread",
        "LoginRequired",
    )


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
    profile = repository.find_customer_profile_by_conversation("conversation-1")
    assert profile is not None
    assert profile.automation_status == "human_owned"
    assert [
        event.event_type
        for event in repository.list_customer_state_events(profile.customer_key)
    ] == ["handoff_opened"]

    assert repository.resolve_handoff_event(events[0].event_id) is True
    assert repository.resolve_handoff_event(events[0].event_id) is False
    assert repository.has_open_handoff("conversation-1") is False
    assert repository.list_open_handoff_events() == []
    released = repository.get_customer_profile(profile.customer_key)
    assert released is not None
    assert released.automation_status == "active"


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

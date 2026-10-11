"""Real-conversation quality capture and cross-computer ZIP export."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
    PlatformSystemEvent,
    PlatformSystemEventKind,
)
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def _snapshot(*messages: tuple[str, MessageDirection, str]) -> ConversationSnapshot:
    return ConversationSnapshot(
        "private-conversation-123",
        tuple(ChatMessage(key, direction, MessageKind.TEXT, text) for key, direction, text in messages),
        platform_product_id="private-product-9",
        product_title="6030 电池",
        displayed_listing_price="240",
    )


def test_quality_capture_attributes_only_verified_application_messages(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime.now(UTC)
    base = _snapshot(
        ("m1", MessageDirection.INCOMING, "6030多少钱？电话 13812345678"),
        ("m2", MessageDirection.OUTGOING, "这个由人工回复"),
    )
    repository.record_quality_snapshot(base, run_id=7, observed_at=now)
    repository.record_quality_snapshot(base, run_id=7, observed_at=now)
    repository.record_quality_send(
        send_id="send-1", run_id=7, conversation_key=base.conversation_key,
        job_id="job-1", mode="auto_send", origin="application_auto",
        draft_text="应用草稿", final_text="应用回复", created_at=now,
    )
    repository.update_quality_send("send-1", status="verified", updated_at=now,
                                   outgoing_message_key="m3")
    repository.record_quality_snapshot(
        _snapshot(
            ("m1", MessageDirection.INCOMING, "6030多少钱？电话 13812345678"),
            ("m2", MessageDirection.OUTGOING, "这个由人工回复"),
            ("m3", MessageDirection.OUTGOING, "应用回复"),
        ), run_id=7, observed_at=now,
    )
    repository.record_quality_send(
        send_id="send-uncertain", run_id=7, conversation_key=base.conversation_key,
        job_id="job-2", mode="auto_send", origin="application_auto",
        draft_text="待确认", final_text="待确认", created_at=now,
    )
    repository.update_quality_send("send-uncertain", status="uncertain", updated_at=now)
    repository.record_quality_snapshot(
        _snapshot(("m4", MessageDirection.OUTGOING, "待确认")), run_id=7,
        observed_at=now,
    )

    with sqlite3.connect(repository.database_path) as connection:
        rows = connection.execute(
            "SELECT message_key, direction, source FROM cs_quality_messages ORDER BY message_key"
        ).fetchall()
    assert rows == [
        ("m1", "incoming", "customer"),
        ("m2", "outgoing", "merchant_external"),
        ("m3", "outgoing", "application_auto"),
        ("m4", "outgoing", "merchant_source_uncertain"),
    ]


def test_quality_export_redacts_identity_and_preserves_battery_numbers(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime.now(UTC)
    repository.record_quality_snapshot(
        _snapshot(("m1", MessageDirection.INCOMING, "6030价格240？电话13812345678")),
        run_id=1, observed_at=now,
    )
    repository.record_quality_send(
        send_id="send-1", run_id=1, conversation_key="private-conversation-123",
        job_id="job-1", mode="human_confirmation", origin="application_reviewed",
        draft_text="原稿", final_text="6030实际价格678元", created_at=now,
    )
    repository.update_quality_send("send-1", status="verified", updated_at=now,
                                   outgoing_message_key="m2")
    repository.record_quality_snapshot(
        _snapshot(("m2", MessageDirection.OUTGOING, "6030实际价格678元")),
        run_id=1, observed_at=now,
    )
    repository.record_quality_snapshot(
        ConversationSnapshot(
            "private-conversation-123", (),
            system_events=(PlatformSystemEvent(
                "ship-1", PlatformSystemEventKind.ORDER_SHIPPED, "你已发货",
            ),),
        ), run_id=1, observed_at=now,
    )
    output = tmp_path / "bundle.zip"
    counts = export_quality_bundle(repository.database_path, output, now=now)
    assert counts["messages"] == 3
    assert counts["sends"] == 1
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        messages = [json.loads(line) for line in archive.read("messages.jsonl").decode().splitlines()]
        manifest = json.loads(archive.read("manifest.json"))
        whole = "\n".join(archive.read(name).decode("utf-8-sig") for name in archive.namelist())
    assert manifest["text_mode"] == "redacted"
    assert "private-conversation-123" not in whole
    assert "13812345678" not in whole
    assert "6030" in whole and "240" in whole and "678" in whole
    assert {item["role"] for item in messages} == {"customer", "merchant", "platform"}
    assert any(item["source"] == "application_reviewed" for item in messages)

    raw_path = tmp_path / "raw.zip"
    export_quality_bundle(repository.database_path, raw_path, include_raw_text=True, now=now)
    with zipfile.ZipFile(raw_path) as archive:
        assert "13812345678" in archive.read("messages.jsonl").decode()


def test_confirmed_non_send_retry_updates_final_text_and_attempt_count(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    now = datetime.now(UTC)
    arguments = {
        "send_id": "retry-1", "run_id": 1, "conversation_key": "conv-1",
        "job_id": "job-1", "mode": "human_confirmation",
        "origin": "application_reviewed", "draft_text": "原稿", "created_at": now,
    }
    repository.record_quality_send(**arguments, final_text="第一次编辑")
    repository.update_quality_send("retry-1", status="not_performed", updated_at=now)
    repository.record_quality_send(**arguments, final_text="第二次编辑")
    with sqlite3.connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT status, final_text, attempt_count FROM cs_quality_sends WHERE send_id = 'retry-1'"
        ).fetchone()
    assert row == ("prepared", "第二次编辑", 2)

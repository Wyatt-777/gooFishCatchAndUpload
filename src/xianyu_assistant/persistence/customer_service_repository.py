"""Independent SQLite persistence for customer-service data."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching
from xianyu_assistant.customer_service.importer import ImportBundle
from xianyu_assistant.customer_service.knowledge_importer import (
    CleanedKnowledgeBundle,
    CleanedKnowledgeDocument,
)
from xianyu_assistant.customer_service.models import (
    ConversationFulfillmentState,
    ConversationSnapshot,
    ConversationSummary,
    CustomerFulfillmentStatus,
    CustomerIntentLevel,
    CustomerLifecycle,
    CustomerOrderStatus,
    CustomerProfile,
    CustomerStateEvent,
    HandoffEvent,
    HistoricalExample,
    MediaAsset,
    PriceChangeDraft,
    PriceChangeStatus,
    ProductKnowledge,
    ReplyDraft,
    ReplyJobStatus,
    SalesStage,
    SalesState,
)
from xianyu_assistant.customer_service.negotiation import NegotiationState
from xianyu_assistant.customer_service.retrieval import HistoricalExampleRetriever


class CustomerServiceRepository:
    """Use short-lived connections so UI and worker threads stay isolated."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        """Expose the local data location for user-visible maintenance controls."""
        return self._database_path

    def initialize(self) -> None:
        """Create the additive customer-service schema and its indexes."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cs_products (
                    product_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform_product_id TEXT,
                    listed_price TEXT,
                    minimum_price TEXT,
                    specifications TEXT NOT NULL DEFAULT '',
                    inventory_notes TEXT NOT NULL DEFAULT '',
                    shipping_notes TEXT NOT NULL DEFAULT '',
                    after_sales_notes TEXT NOT NULL DEFAULT '',
                    supplementary_knowledge TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_product_aliases (
                    product_key TEXT NOT NULL REFERENCES cs_products(product_key) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    PRIMARY KEY (product_key, alias)
                );

                CREATE TABLE IF NOT EXISTS cs_media_assets (
                    asset_id TEXT PRIMARY KEY,
                    product_key TEXT REFERENCES cs_products(product_key) ON DELETE SET NULL,
                    display_name TEXT NOT NULL,
                    scene_tag TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_import_batches (
                    source_sha256 TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    conversation_count INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    example_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_knowledge_import_batches (
                    source_sha256 TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    product_count INTEGER NOT NULL,
                    document_count INTEGER NOT NULL,
                    example_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_knowledge_documents (
                    knowledge_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    product_key TEXT REFERENCES cs_products(product_key) ON DELETE SET NULL,
                    product_category TEXT NOT NULL,
                    chat_intents_json TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_conversation_keys_json TEXT NOT NULL,
                    source_message_keys_json TEXT NOT NULL,
                    quality_flags_json TEXT NOT NULL,
                    usage_note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_training_examples (
                    example_id TEXT PRIMARY KEY,
                    customer_text TEXT NOT NULL,
                    merchant_text TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    customer_fingerprint TEXT NOT NULL,
                    merchant_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_conversations (
                    conversation_key TEXT PRIMARY KEY,
                    display_name TEXT,
                    platform_product_id TEXT,
                    updated_at TEXT NOT NULL,
                    content_expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_messages (
                    id INTEGER PRIMARY KEY,
                    conversation_key TEXT NOT NULL REFERENCES cs_conversations(conversation_key) ON DELETE CASCADE,
                    message_key TEXT NOT NULL,
                    platform_message_id TEXT,
                    direction TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    platform_time TEXT,
                    observed_at TEXT,
                    content_fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_key, message_key)
                );

                CREATE TABLE IF NOT EXISTS cs_reply_jobs (
                    job_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    batch_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_text TEXT,
                    media_asset_id TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(conversation_key, batch_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS cs_handoff_events (
                    id INTEGER PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    handoff_type TEXT NOT NULL DEFAULT 'general',
                    release_on_resolve INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    content_expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_price_change_tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    customer_message_key TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    proposed_price TEXT NOT NULL,
                    minimum_price TEXT,
                    listed_price TEXT,
                    customer_offer TEXT,
                    rationale TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    price_adjustment_key TEXT,
                    price_adjustment_amount TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(conversation_key, customer_message_key)
                );

                CREATE TABLE IF NOT EXISTS cs_run_sessions (
                    id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    stop_reason TEXT,
                    failure_category TEXT,
                    failure_stage TEXT,
                    conversation_ref TEXT,
                    exception_type TEXT,
                    content_expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_processed_fingerprints (
                    batch_fingerprint TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_negotiation_states (
                    conversation_key TEXT PRIMARY KEY,
                    product_key TEXT NOT NULL,
                    price_variant TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    last_customer_offer TEXT,
                    last_counter TEXT,
                    accepted_price TEXT,
                    outcome TEXT,
                    repeated_offer_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_sales_states (
                    conversation_key TEXT PRIMARY KEY,
                    product_key TEXT,
                    stage TEXT NOT NULL,
                    follow_up_text TEXT,
                    follow_up_due_at TEXT,
                    follow_up_count INTEGER NOT NULL DEFAULT 0,
                    last_customer_message_key TEXT,
                    last_merchant_fingerprint TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_conversation_states (
                    conversation_key TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 0,
                    last_turn_id TEXT,
                    last_observed_message_key TEXT,
                    last_handled_message_key TEXT,
                    platform_product_id TEXT,
                    human_owned INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_conversation_fulfillment (
                    conversation_key TEXT PRIMARY KEY,
                    platform_product_id TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    evidence_key TEXT,
                    evidence_text TEXT,
                    shipped_at TEXT,
                    delivered_at TEXT,
                    automation_status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_customer_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    conversation_version INTEGER NOT NULL,
                    message_keys_json TEXT NOT NULL,
                    customer_text TEXT NOT NULL,
                    semantic_json TEXT,
                    decision_json TEXT,
                    reply_text TEXT,
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_send_outbox (
                    send_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    expected_last_message_key TEXT NOT NULL,
                    expected_product_id TEXT,
                    reply_fingerprint TEXT NOT NULL,
                    outgoing_message_key TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_first_contact_catalog (
                    conversation_key TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_customers (
                    customer_key TEXT PRIMARY KEY,
                    platform_customer_id TEXT,
                    primary_conversation_key TEXT NOT NULL,
                    display_name TEXT,
                    lifecycle TEXT NOT NULL DEFAULT 'new',
                    intent_level TEXT NOT NULL DEFAULT 'unknown',
                    sales_stage TEXT,
                    order_status TEXT NOT NULL DEFAULT 'none',
                    fulfillment_status TEXT NOT NULL DEFAULT 'unknown',
                    shipped_at TEXT,
                    delivered_at TEXT,
                    automation_status TEXT NOT NULL DEFAULT 'active',
                    current_product_key TEXT,
                    platform_product_id TEXT,
                    battery_model TEXT,
                    required_range_km TEXT,
                    motor_power_w TEXT,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    budget_amount TEXT,
                    last_customer_offer TEXT,
                    accepted_price TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_customer_message_at TEXT,
                    last_merchant_message_at TEXT,
                    next_follow_up_at TEXT,
                    conversation_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_customer_conversations (
                    conversation_key TEXT PRIMARY KEY,
                    customer_key TEXT NOT NULL REFERENCES cs_customers(customer_key) ON DELETE CASCADE,
                    platform_product_id TEXT,
                    linked_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_customer_state_events (
                    event_key TEXT PRIMARY KEY,
                    customer_key TEXT NOT NULL REFERENCES cs_customers(customer_key) ON DELETE CASCADE,
                    conversation_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_lifecycle TEXT,
                    to_lifecycle TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_maintenance (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_settings (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cs_products_platform_id
                    ON cs_products(platform_product_id);
                CREATE INDEX IF NOT EXISTS idx_cs_knowledge_documents_product
                    ON cs_knowledge_documents(product_key, document_type);
                CREATE INDEX IF NOT EXISTS idx_cs_product_aliases_normalized
                    ON cs_product_aliases(normalized_alias);
                CREATE INDEX IF NOT EXISTS idx_cs_conversations_expires
                    ON cs_conversations(content_expires_at);
                CREATE INDEX IF NOT EXISTS idx_cs_messages_expires
                    ON cs_messages(created_at);
                CREATE INDEX IF NOT EXISTS idx_cs_reply_jobs_expires
                    ON cs_reply_jobs(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cs_handoff_events_expires
                    ON cs_handoff_events(content_expires_at);
                CREATE INDEX IF NOT EXISTS idx_cs_price_change_tasks_updated
                    ON cs_price_change_tasks(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cs_run_sessions_expires
                    ON cs_run_sessions(content_expires_at);
                CREATE INDEX IF NOT EXISTS idx_cs_negotiation_states_updated
                    ON cs_negotiation_states(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cs_sales_states_due
                    ON cs_sales_states(status, follow_up_due_at);
                CREATE INDEX IF NOT EXISTS idx_cs_sales_states_updated
                    ON cs_sales_states(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cs_customer_turns_conversation
                    ON cs_customer_turns(conversation_key, conversation_version);
                CREATE INDEX IF NOT EXISTS idx_cs_customer_turns_updated
                    ON cs_customer_turns(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cs_send_outbox_updated
                    ON cs_send_outbox(updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cs_customers_platform_id
                    ON cs_customers(platform_customer_id)
                    WHERE platform_customer_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_cs_customers_lifecycle
                    ON cs_customers(lifecycle, intent_level, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_cs_customers_follow_up
                    ON cs_customers(next_follow_up_at)
                    WHERE next_follow_up_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_cs_customer_events_customer
                    ON cs_customer_state_events(customer_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_cs_fulfillment_automation
                    ON cs_conversation_fulfillment(status, automation_status);
                """
            )
            # Existing history is promoted into durable profiles without trying
            # to merge identical display names.  The platform session id is the
            # only stable identity currently exposed by the Xianyu page.
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customers (
                    customer_key, primary_conversation_key, display_name,
                    platform_product_id, first_seen_at, last_seen_at,
                    created_at, updated_at
                )
                SELECT conversation_key, conversation_key, display_name,
                       platform_product_id, updated_at, updated_at,
                       updated_at, updated_at
                FROM cs_conversations
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_conversations (
                    conversation_key, customer_key, platform_product_id,
                    linked_at, last_seen_at
                )
                SELECT conversation_key, conversation_key, platform_product_id,
                       updated_at, updated_at
                FROM cs_conversations
                """
            )
            # Existing merchant replies mean this is not a first conversation.
            # Preserve that fact permanently even after 15-day message cleanup.
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_first_contact_catalog (
                    conversation_key, reservation_id, status, reply_fingerprint,
                    created_at, updated_at
                )
                SELECT conversation_key, 'legacy-message', 'sent',
                       'legacy-outgoing-message', MIN(created_at), MAX(created_at)
                FROM cs_messages
                WHERE direction = 'outgoing'
                GROUP BY conversation_key
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_first_contact_catalog (
                    conversation_key, reservation_id, status, reply_fingerprint,
                    created_at, updated_at
                )
                SELECT conversation_key, 'legacy-job', 'sent',
                       'legacy-sent-reply-job', MIN(created_at), MAX(updated_at)
                FROM cs_reply_jobs
                WHERE status = 'sent'
                GROUP BY conversation_key
                """
            )
            reply_job_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cs_reply_jobs)").fetchall()
            }
            if "failure_reason" not in reply_job_columns:
                connection.execute("ALTER TABLE cs_reply_jobs ADD COLUMN failure_reason TEXT")
            price_change_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cs_price_change_tasks)"
                ).fetchall()
            }
            if "price_adjustment_key" not in price_change_columns:
                connection.execute(
                    "ALTER TABLE cs_price_change_tasks ADD COLUMN price_adjustment_key TEXT"
                )
            if "price_adjustment_amount" not in price_change_columns:
                connection.execute(
                    "ALTER TABLE cs_price_change_tasks ADD COLUMN price_adjustment_amount TEXT"
                )
            handoff_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cs_handoff_events)"
                ).fetchall()
            }
            if "handoff_type" not in handoff_columns:
                connection.execute(
                    "ALTER TABLE cs_handoff_events "
                    "ADD COLUMN handoff_type TEXT NOT NULL DEFAULT 'general'"
                )
            if "release_on_resolve" not in handoff_columns:
                connection.execute(
                    "ALTER TABLE cs_handoff_events "
                    "ADD COLUMN release_on_resolve INTEGER NOT NULL DEFAULT 1"
                )
            customer_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cs_customers)").fetchall()
            }
            if "fulfillment_status" not in customer_columns:
                connection.execute(
                    "ALTER TABLE cs_customers "
                    "ADD COLUMN fulfillment_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "delivered_at" not in customer_columns:
                connection.execute(
                    "ALTER TABLE cs_customers ADD COLUMN delivered_at TEXT"
                )
            if "shipped_at" not in customer_columns:
                connection.execute(
                    "ALTER TABLE cs_customers ADD COLUMN shipped_at TEXT"
                )
            fulfillment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cs_conversation_fulfillment)"
                ).fetchall()
            }
            if "shipped_at" not in fulfillment_columns:
                connection.execute(
                    "ALTER TABLE cs_conversation_fulfillment ADD COLUMN shipped_at TEXT"
                )
            run_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cs_run_sessions)"
                ).fetchall()
            }
            for column in (
                "failure_category",
                "failure_stage",
                "conversation_ref",
                "exception_type",
            ):
                if column not in run_columns:
                    connection.execute(
                        f"ALTER TABLE cs_run_sessions ADD COLUMN {column} TEXT"
                    )

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        """Read a non-secret application setting."""
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM cs_settings WHERE name = ?", (name,)).fetchone()
        return default if row is None else str(row["value"])

    def start_run_session(self, *, mode: str, started_at: datetime) -> int:
        """Persist a privacy-safe reception run record for later diagnosis."""
        started = _timestamp(started_at)
        expires = _timestamp(started_at + timedelta(days=15))
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cs_run_sessions (
                    mode, status, started_at, content_expires_at
                ) VALUES (?, 'running', ?, ?)
                """,
                (mode, started, expires),
            )
        return int(cursor.lastrowid)

    def finish_run_session(
        self,
        run_id: int,
        *,
        status: str,
        stopped_at: datetime,
        stop_reason: str | None = None,
        failure_category: str | None = None,
        failure_stage: str | None = None,
        conversation_ref: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        """Finish a reception run without storing chat text or credentials."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE cs_run_sessions
                SET status = ?, stopped_at = ?, stop_reason = ?,
                    failure_category = ?, failure_stage = ?,
                    conversation_ref = ?, exception_type = ?
                WHERE id = ?
                """,
                (
                    status,
                    _timestamp(stopped_at),
                    (stop_reason or "")[:500] or None,
                    (failure_category or "")[:80] or None,
                    (failure_stage or "")[:80] or None,
                    (conversation_ref or "")[:80] or None,
                    (exception_type or "")[:80] or None,
                    run_id,
                ),
            )

    def set_setting(self, name: str, value: str) -> None:
        """Persist a non-secret setting; callers must never pass API keys."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_settings (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (name, value),
            )

    def delete_setting(self, name: str) -> None:
        """Remove one non-secret setting."""
        with self._connection() as connection:
            connection.execute("DELETE FROM cs_settings WHERE name = ?", (name,))

    def observe_customer_summary(
        self,
        summary: ConversationSummary,
        *,
        observed_at: datetime,
        platform_customer_id: str | None = None,
    ) -> CustomerProfile:
        """Create or refresh a durable customer profile from a chat-list row."""
        now = _timestamp(observed_at)
        with self._connection() as connection:
            mapped = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (summary.conversation_key,),
            ).fetchone()
            if mapped is not None:
                customer_key = str(mapped["customer_key"])
            elif platform_customer_id:
                existing = connection.execute(
                    "SELECT customer_key FROM cs_customers WHERE platform_customer_id = ?",
                    (platform_customer_id,),
                ).fetchone()
                customer_key = (
                    str(existing["customer_key"])
                    if existing is not None
                    else f"customer-{platform_customer_id}"
                )
            else:
                customer_key = summary.conversation_key

            connection.execute(
                """
                INSERT INTO cs_customers (
                    customer_key, platform_customer_id, primary_conversation_key,
                    display_name, platform_product_id, first_seen_at, last_seen_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_key) DO UPDATE SET
                    platform_customer_id = COALESCE(
                        excluded.platform_customer_id, cs_customers.platform_customer_id
                    ),
                    display_name = COALESCE(excluded.display_name, cs_customers.display_name),
                    platform_product_id = COALESCE(
                        excluded.platform_product_id, cs_customers.platform_product_id
                    ),
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    customer_key,
                    platform_customer_id,
                    summary.conversation_key,
                    summary.display_name,
                    summary.platform_product_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cs_customer_conversations (
                    conversation_key, customer_key, platform_product_id,
                    linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    customer_key = excluded.customer_key,
                    platform_product_id = COALESCE(
                        excluded.platform_product_id,
                        cs_customer_conversations.platform_product_id
                    ),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    summary.conversation_key,
                    customer_key,
                    summary.platform_product_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE cs_customers
                SET conversation_count = (
                    SELECT COUNT(*) FROM cs_customer_conversations
                    WHERE customer_key = ?
                )
                WHERE customer_key = ?
                """,
                (customer_key, customer_key),
            )
            row = connection.execute(
                "SELECT * FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("顾客档案写入后无法读取。")
        return _customer_profile_from_row(row)

    def record_customer_state(
        self,
        *,
        event_key: str,
        conversation_key: str,
        event_type: str,
        lifecycle: CustomerLifecycle,
        intent_level: CustomerIntentLevel,
        observed_at: datetime,
        product_key: str | None = None,
        platform_product_id: str | None = None,
        sales_stage: SalesStage | None = None,
        order_status: CustomerOrderStatus | None = None,
        automation_status: str | None = None,
        battery_model: str | None = None,
        required_range_km: str | None = None,
        motor_power_w: str | None = None,
        quantity: int | None = None,
        budget_amount: str | None = None,
        last_customer_offer: str | None = None,
        accepted_price: str | None = None,
        next_follow_up_at: datetime | None = None,
        clear_next_follow_up: bool = False,
        payload: dict[str, object] | None = None,
    ) -> CustomerProfile:
        """Apply one idempotent deterministic state transition and audit it."""
        if not event_key.strip() or not conversation_key.strip() or not event_type.strip():
            raise ValueError("顾客状态事件缺少必要标识。")
        if quantity is not None and quantity < 1:
            raise ValueError("顾客意向数量必须大于零。")
        now = _timestamp(observed_at)
        with self._connection() as connection:
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            customer_key = (
                str(mapping["customer_key"])
                if mapping is not None
                else conversation_key
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customers (
                    customer_key, primary_conversation_key, platform_product_id,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_key,
                    conversation_key,
                    platform_product_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_conversations (
                    conversation_key, customer_key, platform_product_id,
                    linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_key, customer_key, platform_product_id, now, now),
            )
            duplicate = connection.execute(
                "SELECT 1 FROM cs_customer_state_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            current = connection.execute(
                "SELECT * FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            if current is None:
                raise RuntimeError("找不到待更新的顾客档案。")
            if duplicate is not None:
                return _customer_profile_from_row(current)

            current_lifecycle = CustomerLifecycle(str(current["lifecycle"]))
            next_lifecycle = _advanced_customer_lifecycle(current_lifecycle, lifecycle)
            current_intent = CustomerIntentLevel(str(current["intent_level"]))
            next_intent = _stronger_customer_intent(current_intent, intent_level)
            follow_up_value = (
                None
                if clear_next_follow_up
                else (
                    _timestamp(next_follow_up_at)
                    if next_follow_up_at is not None
                    else current["next_follow_up_at"]
                )
            )
            connection.execute(
                """
                UPDATE cs_customers SET
                    lifecycle = ?, intent_level = ?,
                    sales_stage = COALESCE(?, sales_stage),
                    order_status = COALESCE(?, order_status),
                    automation_status = COALESCE(?, automation_status),
                    current_product_key = COALESCE(?, current_product_key),
                    platform_product_id = COALESCE(?, platform_product_id),
                    battery_model = COALESCE(?, battery_model),
                    required_range_km = COALESCE(?, required_range_km),
                    motor_power_w = COALESCE(?, motor_power_w),
                    quantity = COALESCE(?, quantity),
                    budget_amount = COALESCE(?, budget_amount),
                    last_customer_offer = COALESCE(?, last_customer_offer),
                    accepted_price = COALESCE(?, accepted_price),
                    last_seen_at = ?, last_customer_message_at = ?,
                    next_follow_up_at = ?, updated_at = ?
                WHERE customer_key = ?
                """,
                (
                    next_lifecycle.value,
                    next_intent.value,
                    sales_stage.value if sales_stage is not None else None,
                    order_status.value if order_status is not None else None,
                    automation_status,
                    product_key,
                    platform_product_id,
                    battery_model,
                    required_range_km,
                    motor_power_w,
                    quantity,
                    budget_amount,
                    last_customer_offer,
                    accepted_price,
                    now,
                    now,
                    follow_up_value,
                    now,
                    customer_key,
                ),
            )
            connection.execute(
                """
                INSERT INTO cs_customer_state_events (
                    event_key, customer_key, conversation_key, event_type,
                    from_lifecycle, to_lifecycle, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    customer_key,
                    conversation_key,
                    event_type,
                    current_lifecycle.value,
                    next_lifecycle.value,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("顾客状态更新后无法读取。")
        return _customer_profile_from_row(row)

    def mark_customer_reply_sent(
        self,
        conversation_key: str,
        *,
        event_key: str,
        sales_stage: SalesStage | None,
        next_follow_up_at: datetime | None,
        sent_at: datetime,
    ) -> None:
        """Record verified merchant activity without inventing customer intent."""
        now = _timestamp(sent_at)
        with self._connection() as connection:
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            customer_key = (
                str(mapping["customer_key"])
                if mapping is not None
                else conversation_key
            )
            current = connection.execute(
                "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            if current is None:
                return
            if connection.execute(
                "SELECT 1 FROM cs_customer_state_events WHERE event_key = ?",
                (event_key,),
            ).fetchone() is not None:
                return
            lifecycle = str(current["lifecycle"])
            connection.execute(
                """
                UPDATE cs_customers SET
                    sales_stage = COALESCE(?, sales_stage),
                    last_seen_at = ?, last_merchant_message_at = ?,
                    next_follow_up_at = ?, updated_at = ?
                WHERE customer_key = ?
                """,
                (
                    sales_stage.value if sales_stage is not None else None,
                    now,
                    now,
                    _timestamp(next_follow_up_at) if next_follow_up_at else None,
                    now,
                    customer_key,
                ),
            )
            connection.execute(
                """
                INSERT INTO cs_customer_state_events (
                    event_key, customer_key, conversation_key, event_type,
                    from_lifecycle, to_lifecycle, payload_json, created_at
                ) VALUES (?, ?, ?, 'merchant_reply_sent', ?, ?, '{}', ?)
                """,
                (event_key, customer_key, conversation_key, lifecycle, lifecycle, now),
            )

    def get_customer_profile(self, customer_key: str) -> CustomerProfile | None:
        """Read one durable customer profile."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
        return None if row is None else _customer_profile_from_row(row)

    def find_customer_profile_by_conversation(
        self, conversation_key: str
    ) -> CustomerProfile | None:
        """Resolve one conversation to its durable customer profile."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT customer.* FROM cs_customers AS customer
                JOIN cs_customer_conversations AS link
                  ON link.customer_key = customer.customer_key
                WHERE link.conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
        return None if row is None else _customer_profile_from_row(row)

    def list_customer_profiles(self, limit: int = 100) -> list[CustomerProfile]:
        """List recently active durable profiles for a future CRM view."""
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cs_customers
                ORDER BY last_seen_at DESC, customer_key
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_customer_profile_from_row(row) for row in rows]

    def list_customer_state_events(
        self, customer_key: str, *, limit: int = 100
    ) -> list[CustomerStateEvent]:
        """List the newest state transitions for one customer."""
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cs_customer_state_events
                WHERE customer_key = ?
                ORDER BY created_at DESC, event_key DESC
                LIMIT ?
                """,
                (customer_key, limit),
            ).fetchall()
        return [
            CustomerStateEvent(
                event_key=str(row["event_key"]),
                customer_key=str(row["customer_key"]),
                conversation_key=str(row["conversation_key"]),
                event_type=str(row["event_type"]),
                from_lifecycle=(
                    CustomerLifecycle(str(row["from_lifecycle"]))
                    if row["from_lifecycle"] is not None
                    else None
                ),
                to_lifecycle=CustomerLifecycle(str(row["to_lifecycle"])),
                payload_json=str(row["payload_json"]),
                created_at=_parse_or_now(str(row["created_at"])),
            )
            for row in rows
        ]

    def should_send_first_contact_catalog(
        self,
        conversation_key: str,
        *,
        snapshot_has_outgoing: bool,
    ) -> bool:
        """Return whether this is a genuinely new customer conversation."""
        if not conversation_key or snapshot_has_outgoing:
            return False
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM cs_first_contact_catalog WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone() is not None:
                return False
            if connection.execute(
                """
                SELECT 1 FROM cs_messages
                WHERE conversation_key = ? AND direction = 'outgoing'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone() is not None:
                return False
            prior_sent_job = connection.execute(
                """
                SELECT 1 FROM cs_reply_jobs
                WHERE conversation_key = ? AND status = 'sent'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone()
        return prior_sent_job is None

    def reserve_first_contact_catalog(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        reply_fingerprint: str,
        created_at: datetime,
    ) -> bool:
        """Reserve the one-time catalog before the atomic combined reply is sent."""
        if not conversation_key or not reservation_id or not reply_fingerprint:
            return False
        now = _timestamp(created_at)
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT reservation_id, status, reply_fingerprint
                FROM cs_first_contact_catalog WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            if existing is not None:
                return (
                    str(existing["reservation_id"]) == reservation_id
                    and str(existing["status"]) == "reserved"
                    and str(existing["reply_fingerprint"]) == reply_fingerprint
                )
            if connection.execute(
                """
                SELECT 1 FROM cs_messages
                WHERE conversation_key = ? AND direction = 'outgoing'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone() is not None:
                return False
            if connection.execute(
                """
                SELECT 1 FROM cs_reply_jobs
                WHERE conversation_key = ? AND status = 'sent'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone() is not None:
                return False
            connection.execute(
                """
                INSERT INTO cs_first_contact_catalog (
                    conversation_key, reservation_id, status, reply_fingerprint,
                    created_at, updated_at
                ) VALUES (?, ?, 'reserved', ?, ?, ?)
                """,
                (conversation_key, reservation_id, reply_fingerprint, now, now),
            )
        return True

    def release_first_contact_catalog_reservation(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
    ) -> None:
        """Release only when the adapter proves that no send action occurred."""
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM cs_first_contact_catalog
                WHERE conversation_key = ? AND reservation_id = ? AND status = 'reserved'
                """,
                (conversation_key, reservation_id),
            )

    def mark_first_contact_catalog_sent(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        updated_at: datetime,
    ) -> None:
        """Permanently close the first-contact reservation after read-back."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE cs_first_contact_catalog
                SET status = 'sent', updated_at = ?
                WHERE conversation_key = ? AND reservation_id = ?
                  AND status = 'reserved'
                """,
                (_timestamp(updated_at), conversation_key, reservation_id),
            )

    def observe_customer_turn(
        self,
        *,
        turn_id: str,
        conversation_key: str,
        message_keys: Sequence[str],
        customer_text: str,
        platform_product_id: str | None,
        observed_at: datetime,
    ) -> int:
        """Persist a complete customer turn and return its monotonic conversation version."""
        if not turn_id or not conversation_key or not message_keys:
            raise ValueError("顾客轮次必须包含会话、轮次ID和消息ID。")
        now = _timestamp(observed_at)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version, last_turn_id FROM cs_conversation_states WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            if row is not None and str(row["last_turn_id"] or "") == turn_id:
                version = int(row["version"])
            else:
                version = (int(row["version"]) if row is not None else 0) + 1
                connection.execute(
                    """
                    INSERT INTO cs_conversation_states (
                        conversation_key, version, last_turn_id, last_observed_message_key,
                        platform_product_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_key) DO UPDATE SET
                        version = excluded.version,
                        last_turn_id = excluded.last_turn_id,
                        last_observed_message_key = excluded.last_observed_message_key,
                        platform_product_id = excluded.platform_product_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        conversation_key,
                        version,
                        turn_id,
                        message_keys[-1],
                        platform_product_id,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO cs_customer_turns (
                    turn_id, conversation_key, conversation_version,
                    message_keys_json, customer_text, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'observed', ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    message_keys_json = excluded.message_keys_json,
                    customer_text = excluded.customer_text,
                    updated_at = excluded.updated_at
                """,
                (
                    turn_id,
                    conversation_key,
                    version,
                    json.dumps(list(message_keys), ensure_ascii=False),
                    customer_text,
                    now,
                    now,
                ),
            )
        return version

    def get_conversation_version(self, conversation_key: str) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version FROM cs_conversation_states WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        return int(row["version"]) if row is not None else None

    def update_customer_turn(
        self,
        turn_id: str,
        *,
        status: str,
        semantic_json: str | None = None,
        decision_json: str | None = None,
        reply_text: str | None = None,
        failure_reason: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Update the replayable audit record without erasing prior details."""
        now = _timestamp(updated_at)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE cs_customer_turns SET
                    status = ?,
                    semantic_json = COALESCE(?, semantic_json),
                    decision_json = COALESCE(?, decision_json),
                    reply_text = COALESCE(?, reply_text),
                    failure_reason = COALESCE(?, failure_reason),
                    updated_at = ?
                WHERE turn_id = ?
                """,
                (
                    status,
                    semantic_json,
                    decision_json,
                    reply_text,
                    failure_reason,
                    now,
                    turn_id,
                ),
            )

    def reserve_send(
        self,
        *,
        send_id: str,
        turn_id: str,
        conversation_key: str,
        expected_version: int,
        expected_last_message_key: str,
        expected_product_id: str | None,
        reply_fingerprint: str,
        created_at: datetime,
    ) -> bool:
        """Atomically reserve a send only for the still-current conversation version."""
        now = _timestamp(created_at)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version, human_owned FROM cs_conversation_states WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            if row is None or int(row["version"]) != expected_version or int(row["human_owned"]):
                return False
            existing = connection.execute(
                "SELECT status, reply_fingerprint FROM cs_send_outbox WHERE send_id = ?",
                (send_id,),
            ).fetchone()
            if existing is not None:
                return (
                    str(existing["status"]) == "reserved"
                    and str(existing["reply_fingerprint"]) == reply_fingerprint
                )
            connection.execute(
                """
                INSERT INTO cs_send_outbox (
                    send_id, turn_id, conversation_key, expected_version,
                    expected_last_message_key, expected_product_id,
                    reply_fingerprint, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    send_id,
                    turn_id,
                    conversation_key,
                    expected_version,
                    expected_last_message_key,
                    expected_product_id,
                    reply_fingerprint,
                    now,
                    now,
                ),
            )
        return True

    def mark_send_verified(
        self,
        send_id: str,
        *,
        outgoing_message_key: str,
        handled_message_key: str,
        updated_at: datetime,
    ) -> None:
        now = _timestamp(updated_at)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT conversation_key, turn_id FROM cs_send_outbox WHERE send_id = ?",
                (send_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE cs_send_outbox
                SET status = 'sent_verified', outgoing_message_key = ?, updated_at = ?
                WHERE send_id = ?
                """,
                (outgoing_message_key, now, send_id),
            )
            connection.execute(
                """
                UPDATE cs_conversation_states
                SET last_handled_message_key = ?, updated_at = ?
                WHERE conversation_key = ?
                """,
                (handled_message_key, now, str(row["conversation_key"])),
            )
            connection.execute(
                """
                UPDATE cs_customer_turns
                SET status = 'sent', updated_at = ? WHERE turn_id = ?
                """,
                (now, str(row["turn_id"])),
            )

    def save_sales_state(self, state: SalesState) -> None:
        """Persist the latest sales stage and replace any older pending follow-up."""
        updated_at = state.updated_at or datetime.now().astimezone()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_sales_states (
                    conversation_key, product_key, stage, follow_up_text,
                    follow_up_due_at, follow_up_count, last_customer_message_key,
                    last_merchant_fingerprint, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    product_key = excluded.product_key,
                    stage = excluded.stage,
                    follow_up_text = excluded.follow_up_text,
                    follow_up_due_at = excluded.follow_up_due_at,
                    follow_up_count = excluded.follow_up_count,
                    last_customer_message_key = excluded.last_customer_message_key,
                    last_merchant_fingerprint = excluded.last_merchant_fingerprint,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    state.conversation_key,
                    state.product_key,
                    state.stage.value,
                    state.follow_up_text,
                    _timestamp(state.follow_up_due_at) if state.follow_up_due_at else None,
                    state.follow_up_count,
                    state.last_customer_message_key,
                    state.last_merchant_fingerprint,
                    state.status,
                    _timestamp(updated_at),
                ),
            )

    def cancel_sales_follow_up(self, conversation_key: str, *, updated_at: datetime) -> None:
        """Cancel only a still-pending follow-up; completed history remains auditable."""
        now = _timestamp(updated_at)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE cs_sales_states
                SET status = 'cancelled', follow_up_text = NULL,
                    follow_up_due_at = NULL, updated_at = ?
                WHERE conversation_key = ? AND status = 'pending'
                """,
                (now, conversation_key),
            )
            connection.execute(
                """
                UPDATE cs_customers
                SET next_follow_up_at = NULL, updated_at = ?
                WHERE customer_key = (
                    SELECT customer_key FROM cs_customer_conversations
                    WHERE conversation_key = ?
                )
                """,
                (now, conversation_key),
            )

    def list_due_sales_follow_ups(
        self, *, now: datetime, limit: int
    ) -> list[SalesState]:
        """Load bounded pending follow-ups in due-time order."""
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT conversation_key, product_key, stage, follow_up_text,
                       follow_up_due_at, follow_up_count, last_customer_message_key,
                       last_merchant_fingerprint, status, updated_at
                FROM cs_sales_states
                WHERE status = 'pending' AND follow_up_count = 0
                  AND follow_up_due_at IS NOT NULL AND follow_up_due_at <= ?
                ORDER BY follow_up_due_at, conversation_key
                LIMIT ?
                """,
                (_timestamp(now), limit),
            ).fetchall()
        return [
            SalesState(
                conversation_key=str(row["conversation_key"]),
                product_key=row["product_key"],
                stage=SalesStage(str(row["stage"])),
                follow_up_text=str(row["follow_up_text"]),
                follow_up_due_at=_parse_or_now(str(row["follow_up_due_at"])),
                follow_up_count=int(row["follow_up_count"]),
                last_customer_message_key=row["last_customer_message_key"],
                last_merchant_fingerprint=row["last_merchant_fingerprint"],
                status=str(row["status"]),
                updated_at=_parse_or_now(str(row["updated_at"])),
            )
            for row in rows
        ]

    def mark_sales_follow_up_sent(
        self,
        conversation_key: str,
        *,
        merchant_fingerprint: str,
        updated_at: datetime,
    ) -> None:
        """Close one due item only after adapter readback verified the send."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE cs_sales_states
                SET stage = ?, follow_up_count = follow_up_count + 1,
                    follow_up_text = NULL, follow_up_due_at = NULL,
                    last_merchant_fingerprint = ?, status = 'followed_up', updated_at = ?
                WHERE conversation_key = ? AND status = 'pending' AND follow_up_count = 0
                """,
                (
                    SalesStage.FOLLOWED_UP.value,
                    merchant_fingerprint,
                    _timestamp(updated_at),
                    conversation_key,
                ),
            )

    def save_negotiation_state(
        self,
        conversation_key: str,
        state: NegotiationState,
        *,
        price_variant: str,
    ) -> None:
        """Persist the latest deterministic negotiation state for restart recovery."""
        if not conversation_key.strip() or not price_variant.strip():
            raise ValueError("议价状态必须包含会话和价格版本。")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_negotiation_states (
                    conversation_key, product_key, price_variant, quantity,
                    last_customer_offer, last_counter, accepted_price, outcome,
                    repeated_offer_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    product_key = excluded.product_key,
                    price_variant = excluded.price_variant,
                    quantity = excluded.quantity,
                    last_customer_offer = excluded.last_customer_offer,
                    last_counter = excluded.last_counter,
                    accepted_price = excluded.accepted_price,
                    outcome = excluded.outcome,
                    repeated_offer_count = excluded.repeated_offer_count,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_key,
                    state.product_key,
                    price_variant,
                    state.quantity,
                    state.last_customer_offer,
                    state.last_counter,
                    state.accepted_price,
                    state.outcome,
                    state.repeated_offer_count,
                    _timestamp(),
                ),
            )

    def load_negotiation_state(
        self, conversation_key: str
    ) -> tuple[NegotiationState, str] | None:
        """Load one conversation's latest negotiation state and price variant."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT product_key, price_variant, quantity, last_customer_offer,
                       last_counter, accepted_price, outcome, repeated_offer_count
                FROM cs_negotiation_states
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
        if row is None:
            return None
        return (
            NegotiationState(
                product_key=str(row["product_key"]),
                quantity=int(row["quantity"]),
                last_customer_offer=row["last_customer_offer"],
                last_counter=row["last_counter"],
                accepted_price=row["accepted_price"],
                outcome=row["outcome"],
                repeated_offer_count=int(row["repeated_offer_count"]),
            ),
            str(row["price_variant"]),
        )

    def save_product_knowledge(self, product: ProductKnowledge) -> None:
        """Create or update one user-maintained product knowledge entry."""
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_products (
                    product_key, name, platform_product_id, listed_price, minimum_price,
                    specifications, inventory_notes, shipping_notes, after_sales_notes,
                    supplementary_knowledge, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    name = excluded.name,
                    platform_product_id = excluded.platform_product_id,
                    listed_price = excluded.listed_price,
                    minimum_price = excluded.minimum_price,
                    specifications = excluded.specifications,
                    inventory_notes = excluded.inventory_notes,
                    shipping_notes = excluded.shipping_notes,
                    after_sales_notes = excluded.after_sales_notes,
                    supplementary_knowledge = excluded.supplementary_knowledge,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    product.product_key,
                    product.name,
                    product.platform_product_id,
                    product.listed_price,
                    product.minimum_price,
                    product.specifications,
                    product.inventory_notes,
                    product.shipping_notes,
                    product.after_sales_notes,
                    product.supplementary_knowledge,
                    int(product.enabled),
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM cs_product_aliases WHERE product_key = ?", (product.product_key,)
            )
            connection.executemany(
                """
                INSERT INTO cs_product_aliases (product_key, alias, normalized_alias)
                VALUES (?, ?, ?)
                """,
                [
                    (product.product_key, alias, normalize_for_matching(alias))
                    for alias in dict.fromkeys(product.aliases)
                    if alias.strip()
                ],
            )

    def list_product_knowledge(self, *, include_disabled: bool = True) -> list[ProductKnowledge]:
        """List product knowledge for the settings UI."""
        condition = "" if include_disabled else " WHERE enabled = 1"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM cs_products{condition} ORDER BY name, product_key"
            ).fetchall()
            return [self._product_from_row(connection, row) for row in rows]

    def archive_products_except(self, current_product_keys: Sequence[str]) -> int:
        """Disable superseded imported rows while preserving user-created products."""
        keys = tuple(dict.fromkeys(key for key in current_product_keys if key.strip()))
        with self._connection() as connection:
            if not keys:
                cursor = connection.execute(
                    "UPDATE cs_products SET enabled = 0 "
                    "WHERE enabled = 1 AND product_key LIKE 'xianyu-%'"
                )
            else:
                placeholders = ",".join("?" for _ in keys)
                cursor = connection.execute(
                    f"UPDATE cs_products SET enabled = 0 "
                    f"WHERE enabled = 1 AND product_key LIKE 'xianyu-%' "
                    f"AND product_key NOT IN ({placeholders})",
                    keys,
                )
        return cursor.rowcount

    def delete_product_knowledge(self, product_key: str) -> None:
        """Delete one product and its aliases; media is made unassociated."""
        with self._connection() as connection:
            connection.execute("DELETE FROM cs_products WHERE product_key = ?", (product_key,))

    def save_media_asset(self, asset: MediaAsset) -> None:
        """Persist one registered image and its current content hash."""
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_media_assets (
                    asset_id, product_key, display_name, scene_tag, description,
                    path, sha256, mime_type, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    product_key = excluded.product_key,
                    display_name = excluded.display_name,
                    scene_tag = excluded.scene_tag,
                    description = excluded.description,
                    path = excluded.path,
                    sha256 = excluded.sha256,
                    mime_type = excluded.mime_type,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    asset.product_key,
                    asset.display_name,
                    asset.scene_tag,
                    asset.description,
                    str(asset.path),
                    asset.sha256,
                    asset.mime_type,
                    int(asset.enabled),
                    now,
                    now,
                ),
            )

    def list_media_assets(self, *, include_disabled: bool = True) -> list[MediaAsset]:
        """List configured local images for the settings UI."""
        condition = "" if include_disabled else " WHERE enabled = 1"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM cs_media_assets{condition} ORDER BY display_name, asset_id"
            ).fetchall()
        return [
            MediaAsset(
                asset_id=str(row["asset_id"]),
                product_key=row["product_key"],
                display_name=str(row["display_name"]),
                scene_tag=str(row["scene_tag"]),
                description=str(row["description"]),
                path=Path(str(row["path"])),
                sha256=str(row["sha256"]),
                mime_type=str(row["mime_type"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def delete_media_asset(self, asset_id: str) -> None:
        """Remove one registered local image resource."""
        with self._connection() as connection:
            connection.execute("DELETE FROM cs_media_assets WHERE asset_id = ?", (asset_id,))

    def has_import_batch(self, source_sha256: str) -> bool:
        """Return whether a source hash has already been imported."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM cs_import_batches WHERE source_sha256 = ?", (source_sha256,)
            ).fetchone()
        return row is not None

    def store_import(self, bundle: ImportBundle) -> bool:
        """Store a redacted import once; return False for an existing source hash."""
        imported_at = _timestamp()
        preview = bundle.preview
        with self._connection() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO cs_import_batches (
                    source_sha256, source_name, conversation_count, message_count,
                    example_count, error_count, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.source_sha256,
                    bundle.source_name,
                    preview.conversation_count,
                    preview.message_count,
                    preview.example_count,
                    preview.error_count,
                    imported_at,
                ),
            ).rowcount
            if inserted != 1:
                return False
            for conversation in bundle.conversations:
                expires_at = _timestamp(_parse_or_now(imported_at) + timedelta(days=15))
                connection.execute(
                    """
                    INSERT INTO cs_conversations (
                        conversation_key, display_name, platform_product_id, updated_at, content_expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_key) DO UPDATE SET
                        display_name = excluded.display_name,
                        platform_product_id = excluded.platform_product_id,
                        updated_at = excluded.updated_at,
                        content_expires_at = excluded.content_expires_at
                    """,
                    (
                        conversation.conversation_key,
                        conversation.display_name,
                        conversation.platform_product_id,
                        imported_at,
                        expires_at,
                    ),
                )
                for message in conversation.messages:
                    connection.execute(
                        """
                        INSERT INTO cs_messages (
                            conversation_key, message_key, platform_message_id, direction,
                            message_type, text, platform_time, observed_at,
                            content_fingerprint, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(conversation_key, message_key) DO UPDATE SET
                            platform_message_id = excluded.platform_message_id,
                            direction = excluded.direction,
                            message_type = excluded.message_type,
                            text = excluded.text,
                            platform_time = excluded.platform_time,
                            observed_at = excluded.observed_at,
                            content_fingerprint = excluded.content_fingerprint
                        """,
                        (
                            conversation.conversation_key,
                            message.message_key,
                            message.platform_message_id,
                            message.direction.value,
                            message.kind.value,
                            message.text,
                            message.platform_time,
                            message.observed_at,
                            message.fingerprint,
                            imported_at,
                        ),
                    )
            for example in bundle.examples:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cs_training_examples (
                        example_id, customer_text, merchant_text, trust_level,
                        customer_fingerprint, merchant_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        example.example_id,
                        example.customer_text,
                        example.merchant_text,
                        example.trust_level,
                        _fingerprint(example.customer_text),
                        _fingerprint(example.merchant_text),
                        imported_at,
                    ),
                )
        return True

    def has_cleaned_knowledge_batch(self, source_sha256: str) -> bool:
        """Return whether a cleaned knowledge file has already been imported."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM cs_knowledge_import_batches WHERE source_sha256 = ?",
                (source_sha256,),
            ).fetchone()
        return row is not None

    def store_cleaned_knowledge(self, bundle: CleanedKnowledgeBundle) -> bool:
        """Atomically merge a cleaned knowledge bundle once by source hash.

        Existing non-empty product facts and enabled state are preserved because
        they may contain later human edits. Imported aliases are added rather than
        replacing the user's alias set.
        """
        imported_at = _timestamp()
        preview = bundle.preview
        with self._connection() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO cs_knowledge_import_batches (
                    source_sha256, source_name, product_count, document_count,
                    example_count, error_count, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.source_sha256,
                    bundle.source_name,
                    preview.product_count,
                    preview.document_count,
                    preview.example_count,
                    preview.error_count,
                    imported_at,
                ),
            ).rowcount
            if inserted != 1:
                return False
            for product in bundle.products:
                existing = connection.execute(
                    "SELECT * FROM cs_products WHERE product_key = ?",
                    (product.product_key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO cs_products (
                            product_key, name, platform_product_id, listed_price, minimum_price,
                            specifications, inventory_notes, shipping_notes, after_sales_notes,
                            supplementary_knowledge, enabled, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                        """,
                        (
                            product.product_key,
                            product.name,
                            product.platform_product_id,
                            product.listed_price,
                            product.minimum_price,
                            product.specifications,
                            product.inventory_notes,
                            product.shipping_notes,
                            product.after_sales_notes,
                            int(product.enabled),
                            imported_at,
                            imported_at,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE cs_products SET
                            platform_product_id = COALESCE(NULLIF(platform_product_id, ''), ?),
                            listed_price = COALESCE(NULLIF(listed_price, ''), ?),
                            minimum_price = COALESCE(NULLIF(minimum_price, ''), ?),
                            specifications = CASE WHEN specifications = '' THEN ? ELSE specifications END,
                            inventory_notes = CASE WHEN inventory_notes = '' THEN ? ELSE inventory_notes END,
                            shipping_notes = CASE WHEN shipping_notes = '' THEN ? ELSE shipping_notes END,
                            after_sales_notes = CASE WHEN after_sales_notes = '' THEN ? ELSE after_sales_notes END,
                            updated_at = ?
                        WHERE product_key = ?
                        """,
                        (
                            product.platform_product_id,
                            product.listed_price,
                            product.minimum_price,
                            product.specifications,
                            product.inventory_notes,
                            product.shipping_notes,
                            product.after_sales_notes,
                            imported_at,
                            product.product_key,
                        ),
                    )
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO cs_product_aliases
                        (product_key, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (product.product_key, alias, normalize_for_matching(alias))
                        for alias in product.aliases
                        if alias.strip()
                    ],
                )
            known_product_keys = {
                str(row["product_key"])
                for row in connection.execute("SELECT product_key FROM cs_products").fetchall()
            }
            for document in bundle.documents:
                product_key = (
                    document.product_key if document.product_key in known_product_keys else None
                )
                connection.execute(
                    """
                    INSERT INTO cs_knowledge_documents (
                        knowledge_id, source_sha256, document_type, product_key,
                        product_category, chat_intents_json, content,
                        source_conversation_keys_json, source_message_keys_json,
                        quality_flags_json, usage_note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(knowledge_id) DO UPDATE SET
                        source_sha256 = excluded.source_sha256,
                        document_type = excluded.document_type,
                        product_key = excluded.product_key,
                        product_category = excluded.product_category,
                        chat_intents_json = excluded.chat_intents_json,
                        content = excluded.content,
                        source_conversation_keys_json = excluded.source_conversation_keys_json,
                        source_message_keys_json = excluded.source_message_keys_json,
                        quality_flags_json = excluded.quality_flags_json,
                        usage_note = excluded.usage_note,
                        updated_at = excluded.updated_at
                    """,
                    (
                        document.knowledge_id,
                        bundle.source_sha256,
                        document.document_type,
                        product_key,
                        document.product_category,
                        json.dumps(document.chat_intents, ensure_ascii=False),
                        document.content,
                        json.dumps(document.source_conversation_keys, ensure_ascii=False),
                        json.dumps(document.source_message_keys, ensure_ascii=False),
                        json.dumps(document.quality_flags, ensure_ascii=False),
                        document.usage_note,
                        imported_at,
                        imported_at,
                    ),
                )
            for example in bundle.examples:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cs_training_examples (
                        example_id, customer_text, merchant_text, trust_level,
                        customer_fingerprint, merchant_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        example.example_id,
                        example.customer_text,
                        example.merchant_text,
                        example.trust_level,
                        _fingerprint(example.customer_text),
                        _fingerprint(example.merchant_text),
                        imported_at,
                    ),
                )
        return True

    def list_cleaned_knowledge_documents(self) -> list[CleanedKnowledgeDocument]:
        """Load cleaned documents with their source identifiers for audit or retrieval."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cs_knowledge_documents ORDER BY knowledge_id"
            ).fetchall()
        return [
            CleanedKnowledgeDocument(
                knowledge_id=str(row["knowledge_id"]),
                document_type=str(row["document_type"]),
                content=str(row["content"]),
                product_key=row["product_key"],
                product_category=str(row["product_category"]),
                chat_intents=tuple(json.loads(str(row["chat_intents_json"]))),
                source_conversation_keys=tuple(
                    json.loads(str(row["source_conversation_keys_json"]))
                ),
                source_message_keys=tuple(json.loads(str(row["source_message_keys_json"]))),
                quality_flags=tuple(json.loads(str(row["quality_flags_json"]))),
                usage_note=str(row["usage_note"]),
            )
            for row in rows
        ]

    def store_live_snapshot(
        self,
        snapshot: ConversationSnapshot,
        *,
        display_name: str | None = None,
        examples: Sequence[HistoricalExample] = (),
    ) -> int:
        """Upsert one read-only page snapshot and its local wording examples."""
        observed_at = _timestamp()
        expires_at = _timestamp(_parse_or_now(observed_at) + timedelta(days=15))
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_conversations (
                    conversation_key, display_name, platform_product_id, updated_at, content_expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    platform_product_id = excluded.platform_product_id,
                    updated_at = excluded.updated_at,
                    content_expires_at = excluded.content_expires_at
                """,
                (
                    snapshot.conversation_key,
                    display_name,
                    snapshot.platform_product_id,
                    observed_at,
                    expires_at,
                ),
            )
            for message in snapshot.messages:
                connection.execute(
                    """
                    INSERT INTO cs_messages (
                        conversation_key, message_key, platform_message_id, direction,
                        message_type, text, platform_time, observed_at,
                        content_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_key, message_key) DO UPDATE SET
                        direction = excluded.direction,
                        message_type = excluded.message_type,
                        text = excluded.text,
                        platform_time = excluded.platform_time,
                        observed_at = excluded.observed_at,
                        content_fingerprint = excluded.content_fingerprint
                    """,
                    (
                        snapshot.conversation_key,
                        message.message_key,
                        message.message_key,
                        message.direction.value,
                        message.kind.value,
                        message.text or "",
                        _timestamp(message.platform_time) if message.platform_time else None,
                        _timestamp(message.observed_at) if message.observed_at else observed_at,
                        message.content_fingerprint or _fingerprint(message.text or ""),
                        observed_at,
                    ),
                )
            for example in examples:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cs_training_examples (
                        example_id, customer_text, merchant_text, trust_level,
                        customer_fingerprint, merchant_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        example.example_id,
                        example.customer_text,
                        example.merchant_text,
                        example.trust_level,
                        _fingerprint(example.customer_text),
                        _fingerprint(example.merchant_text),
                        observed_at,
                    ),
                )
        self.observe_customer_summary(
            ConversationSummary(
                conversation_key=snapshot.conversation_key,
                display_name=display_name,
                platform_product_id=snapshot.platform_product_id,
                product_title=snapshot.product_title,
            ),
            observed_at=_parse_or_now(observed_at),
        )
        return len(snapshot.messages)

    def store_live_summary(self, summary: ConversationSummary) -> None:
        """Persist one redacted conversation index row before full-body reading."""
        now = _timestamp()
        expires_at = _timestamp(_parse_or_now(now) + timedelta(days=15))
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_conversations (
                    conversation_key, display_name, platform_product_id, updated_at, content_expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    platform_product_id = excluded.platform_product_id,
                    updated_at = excluded.updated_at,
                    content_expires_at = excluded.content_expires_at
                """,
                (
                    summary.conversation_key,
                    summary.display_name,
                    summary.platform_product_id,
                    now,
                    expires_at,
                ),
            )
        self.observe_customer_summary(
            summary,
            observed_at=_parse_or_now(now),
        )

    def list_historical_examples(self, *, limit: int | None = None) -> list[HistoricalExample]:
        """Load only persisted examples; auto-generated rows are excluded."""
        query = """
            SELECT example_id, customer_text, merchant_text, trust_level
            FROM cs_training_examples
            WHERE trust_level != 'auto_generated'
            ORDER BY created_at, example_id
        """
        parameters: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            HistoricalExample(
                example_id=str(row["example_id"]),
                customer_text=str(row["customer_text"]),
                merchant_text=str(row["merchant_text"]),
                trust_level=str(row["trust_level"]),
            )
            for row in rows
        ]

    def find_historical_examples(self, query: str, limit: int) -> list[HistoricalExample]:
        """Rank the full local corpus so recent cleaned examples are reachable."""
        examples = [
            example
            for example in self.list_historical_examples()
            if example.trust_level != "user_curated"
        ]
        retriever = HistoricalExampleRetriever(examples)
        return [item.example for item in retriever.search(query, limit=limit)]

    def find_curated_knowledge_answers(
        self, query: str, limit: int
    ) -> list[HistoricalExample]:
        """Rank only facts explicitly curated by the user."""
        examples = [
            example
            for example in self.list_historical_examples()
            if example.trust_level == "user_curated"
        ]
        retriever = HistoricalExampleRetriever(examples)
        return [item.example for item in retriever.search(query, limit=limit)]

    def find_knowledge_answers(self, query: str, limit: int) -> list[HistoricalExample]:
        """Rank all reviewed Q&A knowledge, keeping raw chat history style-only."""
        if limit <= 0:
            return []
        knowledge_levels = {"user_curated", "conversation_knowledge", "cleaned_history"}
        examples = [
            example
            for example in self.list_historical_examples()
            if example.trust_level in knowledge_levels
        ]
        retriever = HistoricalExampleRetriever(examples)
        ranked = [
            item.example
            for item in retriever.search(query, limit=max(limit * 4, 20))
        ]
        normalized_query = normalize_for_matching(query)
        enabled_products = self.list_product_knowledge(include_disabled=False)
        product_aliases: dict[str, set[str]] = {}
        products = []
        for candidate in enabled_products:
            candidates = {
                normalize_for_matching(candidate.name),
                *(normalize_for_matching(alias) for alias in candidate.aliases),
            }
            product_aliases[candidate.product_key] = {
                alias for alias in candidates if len(alias) >= 4
            }
            if any(
                len(alias) >= 4 and alias in normalized_query
                for alias in candidates
            ):
                products.append(candidate)
        if not products:
            return ranked[:limit]
        aliases = {
            normalize_for_matching(value)
            for product in products
            for value in (product.name, *product.aliases)
            if len(normalize_for_matching(value)) >= 4
        }
        target_keys = {product.product_key for product in products}

        def mentions_target(example: HistoricalExample) -> bool:
            text = normalize_for_matching(
                f"{example.customer_text} {example.merchant_text}"
            )
            return any(alias in text for alias in aliases)

        def mentions_other_battery_model(example: HistoricalExample) -> bool:
            text = normalize_for_matching(
                f"{example.customer_text} {example.merchant_text}"
            )
            mentioned_keys = {
                product_key
                for product_key, known_aliases in product_aliases.items()
                if any(alias in text for alias in known_aliases)
            }
            if mentioned_keys - target_keys:
                return True
            mentions_generic_model = bool(
                re.search(r"(?:48|60|72)v\d+(?:ah|a)?", text)
                or re.search(r"(?<!\d)(?:6020|6030|4830)(?!\d)", text)
            )
            return mentions_generic_model and not mentions_target(example)

        relevant = [item for item in ranked if not mentions_other_battery_model(item)]
        current = [
            item
            for item in relevant
            if item.example_id.startswith("current-") and mentions_target(item)
        ]
        others = [item for item in relevant if item not in current]
        return (current + others)[:limit]

    def find_product_knowledge(
        self,
        *,
        platform_product_id: str | None = None,
        normalized_title: str | None = None,
    ) -> ProductKnowledge | None:
        """Find an enabled product by exact platform ID or normalized name/alias."""
        with self._connection() as connection:
            row = None
            if platform_product_id:
                row = connection.execute(
                    "SELECT * FROM cs_products WHERE platform_product_id = ? AND enabled = 1",
                    (platform_product_id,),
                ).fetchone()
            if row is None and normalized_title:
                candidates = connection.execute(
                    "SELECT * FROM cs_products WHERE enabled = 1 ORDER BY product_key"
                ).fetchall()
                for candidate in candidates:
                    aliases = connection.execute(
                        "SELECT normalized_alias FROM cs_product_aliases WHERE product_key = ?",
                        (str(candidate["product_key"]),),
                    ).fetchall()
                    if normalize_for_matching(str(candidate["name"])) == normalized_title or any(
                        str(alias["normalized_alias"]) == normalized_title for alias in aliases
                    ):
                        row = candidate
                        break
            if row is None:
                return None
            return self._product_from_row(connection, row)

    def find_product_knowledge_for_query(self, query: str) -> ProductKnowledge | None:
        """Resolve a current product from an explicit model/alias in customer text."""
        normalized_query = normalize_for_matching(query)
        if not normalized_query:
            return None
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM cs_products WHERE enabled = 1 ORDER BY product_key"
            ).fetchall()
            matches: list[tuple[int, sqlite3.Row]] = []
            for row in rows:
                aliases = connection.execute(
                    "SELECT normalized_alias FROM cs_product_aliases WHERE product_key = ?",
                    (str(row["product_key"]),),
                ).fetchall()
                candidates = {
                    normalize_for_matching(str(row["name"])),
                    *(str(alias["normalized_alias"]) for alias in aliases),
                }
                matched_lengths = [
                    len(candidate)
                    for candidate in candidates
                    if len(candidate) >= 4 and candidate in normalized_query
                ]
                if matched_lengths:
                    matches.append((max(matched_lengths), row))
            if not matches:
                return None
            best_length = max(score for score, _row in matches)
            best = [row for score, row in matches if score == best_length]
            if len(best) != 1:
                return None
            return self._product_from_row(connection, best[0])

    def get_product_knowledge(self, product_key: str) -> ProductKnowledge | None:
        """Load one enabled product by its stable local key."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cs_products WHERE product_key = ? AND enabled = 1",
                (product_key,),
            ).fetchone()
            return None if row is None else self._product_from_row(connection, row)

    @staticmethod
    def _product_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> ProductKnowledge:
        aliases = connection.execute(
            "SELECT alias FROM cs_product_aliases WHERE product_key = ? ORDER BY alias",
            (str(row["product_key"]),),
        ).fetchall()
        return ProductKnowledge(
            product_key=str(row["product_key"]),
            name=str(row["name"]),
            aliases=tuple(str(alias["alias"]) for alias in aliases),
            platform_product_id=row["platform_product_id"],
            listed_price=row["listed_price"],
            minimum_price=row["minimum_price"],
            specifications=str(row["specifications"]),
            inventory_notes=str(row["inventory_notes"]),
            shipping_notes=str(row["shipping_notes"]),
            after_sales_notes=str(row["after_sales_notes"]),
            supplementary_knowledge=str(row["supplementary_knowledge"]),
            enabled=bool(row["enabled"]),
        )

    def get_media_asset(self, asset_id: str) -> MediaAsset | None:
        """Return one configured image asset without reading the file."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cs_media_assets WHERE asset_id = ? AND enabled = 1", (asset_id,)
            ).fetchone()
        if row is None:
            return None
        return MediaAsset(
            asset_id=str(row["asset_id"]),
            product_key=row["product_key"],
            display_name=str(row["display_name"]),
            scene_tag=str(row["scene_tag"]),
            description=str(row["description"]),
            path=Path(str(row["path"])),
            sha256=str(row["sha256"]),
            mime_type=str(row["mime_type"]),
            enabled=bool(row["enabled"]),
        )

    def save_reply_draft(self, draft: ReplyDraft) -> None:
        """Persist a sanitized draft with a 15-day content expiry."""
        now = datetime.now().astimezone()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_reply_jobs (
                    job_id, conversation_key, batch_fingerprint, status,
                    draft_text, media_asset_id, failure_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    draft_text = excluded.draft_text,
                    media_asset_id = excluded.media_asset_id,
                    failure_reason = excluded.failure_reason,
                    updated_at = excluded.updated_at
                ON CONFLICT(conversation_key, batch_fingerprint) DO UPDATE SET
                    job_id = excluded.job_id,
                    status = excluded.status,
                    draft_text = excluded.draft_text,
                    media_asset_id = excluded.media_asset_id,
                    failure_reason = excluded.failure_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    draft.job_id,
                    draft.conversation_key,
                    draft.batch_fingerprint,
                    draft.status.value,
                    draft.reply_text,
                    draft.media_asset_id,
                    draft.failure_reason,
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )

    def save_price_change_draft(self, draft: PriceChangeDraft) -> None:
        """Persist a fixed-price proposal with a 15-day audit lifetime."""
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_price_change_tasks (
                    task_id, conversation_key, customer_message_key, product_key,
                    product_name, proposed_price, minimum_price, listed_price,
                    customer_offer, rationale, status, failure_reason,
                    price_adjustment_key, price_adjustment_amount, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    proposed_price = excluded.proposed_price,
                    minimum_price = excluded.minimum_price,
                    listed_price = excluded.listed_price,
                    customer_offer = excluded.customer_offer,
                    rationale = excluded.rationale,
                    status = excluded.status,
                    failure_reason = excluded.failure_reason,
                    price_adjustment_key = excluded.price_adjustment_key,
                    price_adjustment_amount = excluded.price_adjustment_amount,
                    updated_at = excluded.updated_at
                ON CONFLICT(conversation_key, customer_message_key) DO UPDATE SET
                    task_id = excluded.task_id,
                    product_key = excluded.product_key,
                    product_name = excluded.product_name,
                    proposed_price = excluded.proposed_price,
                    minimum_price = excluded.minimum_price,
                    listed_price = excluded.listed_price,
                    customer_offer = excluded.customer_offer,
                    rationale = excluded.rationale,
                    status = excluded.status,
                    failure_reason = excluded.failure_reason,
                    price_adjustment_key = excluded.price_adjustment_key,
                    price_adjustment_amount = excluded.price_adjustment_amount,
                    updated_at = excluded.updated_at
                """,
                (
                    draft.task_id,
                    draft.conversation_key,
                    draft.customer_message_key,
                    draft.product_key,
                    draft.product_name,
                    draft.proposed_price,
                    draft.minimum_price,
                    draft.listed_price,
                    draft.customer_offer,
                    draft.rationale,
                    draft.status.value,
                    draft.failure_reason,
                    draft.price_adjustment_key,
                    draft.price_adjustment_amount,
                    now,
                    now,
                ),
            )

    def list_price_change_drafts(self, limit: int = 100) -> list[PriceChangeDraft]:
        """Return recent price proposals, with actionable entries first."""
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cs_price_change_tasks
                ORDER BY CASE status WHEN 'awaiting_review' THEN 0 WHEN 'applying' THEN 1 ELSE 2 END,
                         updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            PriceChangeDraft(
                task_id=str(row["task_id"]),
                conversation_key=str(row["conversation_key"]),
                customer_message_key=str(row["customer_message_key"]),
                product_key=str(row["product_key"]),
                product_name=str(row["product_name"]),
                proposed_price=str(row["proposed_price"]),
                minimum_price=row["minimum_price"],
                listed_price=row["listed_price"],
                customer_offer=row["customer_offer"],
                rationale=str(row["rationale"] or ""),
                status=PriceChangeStatus(str(row["status"])),
                failure_reason=row["failure_reason"],
                price_adjustment_key=row["price_adjustment_key"],
                price_adjustment_amount=row["price_adjustment_amount"],
            )
            for row in rows
        ]

    def delete_price_change_draft(self, task_id: str) -> bool:
        """Delete one explicitly selected local price-change audit record."""
        normalized = task_id.strip()
        if not normalized:
            return False
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM cs_price_change_tasks WHERE task_id = ?",
                (normalized,),
            )
        return cursor.rowcount > 0

    def find_reply_draft(
        self, *, conversation_key: str, batch_fingerprint: str
    ) -> ReplyDraft | None:
        """Load one persisted job without retaining any additional source content."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT job_id, conversation_key, batch_fingerprint, status,
                       draft_text, media_asset_id, failure_reason
                FROM cs_reply_jobs
                WHERE conversation_key = ? AND batch_fingerprint = ?
                """,
                (conversation_key, batch_fingerprint),
            ).fetchone()
        if row is None:
            return None
        return ReplyDraft(
            job_id=str(row["job_id"]),
            conversation_key=str(row["conversation_key"]),
            batch_fingerprint=str(row["batch_fingerprint"]),
            reply_text=str(row["draft_text"] or ""),
            media_asset_id=row["media_asset_id"],
            status=ReplyJobStatus(str(row["status"])),
            failure_reason=row["failure_reason"],
        )

    def recover_interrupted_reply_jobs(self, *, recovered_at: datetime) -> int:
        """Close abandoned pipeline states without retrying an uncertain send."""
        now = _timestamp(recovered_at)
        expires_at = _timestamp(recovered_at + timedelta(days=15))
        recoverable = (
            ReplyJobStatus.DEBOUNCING.value,
            ReplyJobStatus.READY.value,
            ReplyJobStatus.READING_CONTEXT.value,
            ReplyJobStatus.GENERATING.value,
            ReplyJobStatus.POLICY_CHECK.value,
        )
        placeholders = ",".join("?" for _ in recoverable)
        with self._connection() as connection:
            interrupted = connection.execute(
                f"""
                UPDATE cs_reply_jobs
                SET status = 'failed',
                    failure_reason = '程序上次运行中断，已安全重新排队。',
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (now, *recoverable),
            )
            uncertain_rows = connection.execute(
                "SELECT conversation_key FROM cs_reply_jobs WHERE status = 'sending'"
            ).fetchall()
            sending = connection.execute(
                """
                UPDATE cs_reply_jobs
                SET status = 'handoff',
                    failure_reason = '程序在发送确认阶段中断，禁止自动重发，请人工核对。',
                    updated_at = ?
                WHERE status = 'sending'
                """,
                (now,),
            )
            for row in uncertain_rows:
                conversation_key = str(row["conversation_key"])
                connection.execute(
                    """
                    INSERT INTO cs_handoff_events (
                        conversation_key, reason, status, handoff_type,
                        release_on_resolve, created_at, content_expires_at
                    )
                    SELECT ?, ?, 'open', 'general', 1, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM cs_handoff_events
                        WHERE conversation_key = ? AND status = 'open'
                    )
                    """,
                    (
                        conversation_key,
                        "程序在发送确认阶段中断，禁止自动重发，请人工核对。",
                        now,
                        expires_at,
                        conversation_key,
                    ),
                )
        return interrupted.rowcount + sending.rowcount

    def has_prior_reply_job(self, conversation_key: str, *, exclude_job_id: str) -> bool:
        """Check whether a conversation has already reached an earlier question batch."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM cs_reply_jobs
                WHERE conversation_key = ? AND job_id != ?
                LIMIT 1
                """,
                (conversation_key, exclude_job_id),
            ).fetchone()
        return row is not None

    def record_shipped_conversation(
        self,
        *,
        conversation_key: str,
        platform_product_id: str | None,
        evidence_key: str,
        evidence_text: str,
        observed_at: datetime,
    ) -> ConversationFulfillmentState:
        """Persist platform-confirmed shipment and apply a sticky human lock."""
        now = _timestamp(observed_at)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM cs_conversation_fulfillment WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            same_restored_event = bool(
                existing is not None
                and str(existing["status"]) == CustomerFulfillmentStatus.SHIPPED.value
                and str(existing["evidence_key"] or "") == evidence_key
            )
            automation_status = (
                str(existing["automation_status"])
                if same_restored_event
                else "human_owned"
            )
            shipped_at = (
                str(existing["shipped_at"])
                if existing is not None and existing["shipped_at"] is not None
                else now
            )
            connection.execute(
                """
                INSERT INTO cs_conversation_fulfillment (
                    conversation_key, platform_product_id, status,
                    evidence_key, evidence_text, shipped_at,
                    automation_status, updated_at
                ) VALUES (?, ?, 'shipped', ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    platform_product_id = COALESCE(
                        excluded.platform_product_id,
                        cs_conversation_fulfillment.platform_product_id
                    ),
                    status = CASE
                        WHEN cs_conversation_fulfillment.status = 'delivered'
                        THEN 'delivered' ELSE 'shipped' END,
                    evidence_key = excluded.evidence_key,
                    evidence_text = excluded.evidence_text,
                    shipped_at = COALESCE(
                        cs_conversation_fulfillment.shipped_at,
                        excluded.shipped_at
                    ),
                    automation_status = ?,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_key,
                    platform_product_id,
                    evidence_key,
                    evidence_text,
                    shipped_at,
                    automation_status,
                    now,
                    automation_status,
                ),
            )
            connection.execute(
                """
                INSERT INTO cs_conversation_states (
                    conversation_key, platform_product_id, human_owned, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    platform_product_id = COALESCE(
                        excluded.platform_product_id,
                        cs_conversation_states.platform_product_id
                    ),
                    human_owned = excluded.human_owned,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_key,
                    platform_product_id,
                    1 if automation_status == "human_owned" else 0,
                    now,
                ),
            )
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            customer_key = (
                str(mapping["customer_key"])
                if mapping is not None
                else conversation_key
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customers (
                    customer_key, primary_conversation_key, platform_product_id,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_key,
                    conversation_key,
                    platform_product_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_conversations (
                    conversation_key, customer_key, platform_product_id,
                    linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_key, customer_key, platform_product_id, now, now),
            )
            connection.execute(
                """
                UPDATE cs_customers
                SET fulfillment_status = CASE
                        WHEN fulfillment_status = 'delivered'
                        THEN 'delivered' ELSE 'shipped' END,
                    shipped_at = COALESCE(shipped_at, ?),
                    last_seen_at = ?, updated_at = ?
                WHERE customer_key = ?
                """,
                (shipped_at, now, now, customer_key),
            )
            lifecycle_row = connection.execute(
                "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            lifecycle = (
                str(lifecycle_row["lifecycle"])
                if lifecycle_row is not None
                else CustomerLifecycle.NEW.value
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_state_events (
                    event_key, customer_key, conversation_key, event_type,
                    from_lifecycle, to_lifecycle, payload_json, created_at
                ) VALUES (?, ?, ?, 'platform_order_shipped', ?, ?, ?, ?)
                """,
                (
                    f"shipment:{evidence_key}",
                    customer_key,
                    conversation_key,
                    lifecycle,
                    lifecycle,
                    json.dumps(
                        {
                            "platform_product_id": platform_product_id,
                            "evidence_text": evidence_text,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cs_conversation_fulfillment WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("发货状态写入后无法读取。")
        return _fulfillment_state_from_row(row)

    def record_delivered_conversation(
        self,
        *,
        conversation_key: str,
        platform_product_id: str | None,
        evidence_key: str,
        evidence_text: str,
        observed_at: datetime,
    ) -> ConversationFulfillmentState:
        """Persist one platform-confirmed delivery and apply a conversation lock."""
        now = _timestamp(observed_at)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM cs_conversation_fulfillment WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
            automation_status = (
                str(existing["automation_status"])
                if existing is not None
                and str(existing["status"]) == CustomerFulfillmentStatus.DELIVERED.value
                and str(existing["evidence_key"] or "") == evidence_key
                else "human_owned"
            )
            delivered_at = (
                str(existing["delivered_at"])
                if existing is not None and existing["delivered_at"] is not None
                else now
            )
            connection.execute(
                """
                INSERT INTO cs_conversation_fulfillment (
                    conversation_key, platform_product_id, status,
                    evidence_key, evidence_text, delivered_at,
                    automation_status, updated_at
                ) VALUES (?, ?, 'delivered', ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    platform_product_id = COALESCE(
                        excluded.platform_product_id,
                        cs_conversation_fulfillment.platform_product_id
                    ),
                    status = 'delivered',
                    evidence_key = excluded.evidence_key,
                    evidence_text = excluded.evidence_text,
                    delivered_at = COALESCE(
                        cs_conversation_fulfillment.delivered_at,
                        excluded.delivered_at
                    ),
                    automation_status = ?,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_key,
                    platform_product_id,
                    evidence_key,
                    evidence_text,
                    delivered_at,
                    automation_status,
                    now,
                    automation_status,
                ),
            )
            connection.execute(
                """
                INSERT INTO cs_conversation_states (
                    conversation_key, platform_product_id, human_owned, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    platform_product_id = COALESCE(
                        excluded.platform_product_id,
                        cs_conversation_states.platform_product_id
                    ),
                    human_owned = excluded.human_owned,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_key,
                    platform_product_id,
                    1 if automation_status == "human_owned" else 0,
                    now,
                ),
            )
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            customer_key = (
                str(mapping["customer_key"])
                if mapping is not None
                else conversation_key
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customers (
                    customer_key, primary_conversation_key, platform_product_id,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_key,
                    conversation_key,
                    platform_product_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_conversations (
                    conversation_key, customer_key, platform_product_id,
                    linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_key, customer_key, platform_product_id, now, now),
            )
            connection.execute(
                """
                UPDATE cs_customers
                SET fulfillment_status = 'delivered',
                    delivered_at = COALESCE(delivered_at, ?),
                    last_seen_at = ?, updated_at = ?
                WHERE customer_key = ?
                """,
                (delivered_at, now, now, customer_key),
            )
            lifecycle_row = connection.execute(
                "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            lifecycle = (
                str(lifecycle_row["lifecycle"])
                if lifecycle_row is not None
                else CustomerLifecycle.NEW.value
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_state_events (
                    event_key, customer_key, conversation_key, event_type,
                    from_lifecycle, to_lifecycle, payload_json, created_at
                ) VALUES (?, ?, ?, 'platform_order_delivered', ?, ?, ?, ?)
                """,
                (
                    f"delivery:{evidence_key}",
                    customer_key,
                    conversation_key,
                    lifecycle,
                    lifecycle,
                    json.dumps(
                        {
                            "platform_product_id": platform_product_id,
                            "evidence_text": evidence_text,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cs_conversation_fulfillment WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("签收状态写入后无法读取。")
        return _fulfillment_state_from_row(row)

    def get_conversation_fulfillment(
        self, conversation_key: str
    ) -> ConversationFulfillmentState | None:
        """Read durable fulfillment state for one order conversation."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM cs_conversation_fulfillment WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        return _fulfillment_state_from_row(row) if row is not None else None

    def is_post_delivery_human_owned(self, conversation_key: str) -> bool:
        """Return whether a shipped or delivered order remains human-owned."""
        state = self.get_conversation_fulfillment(conversation_key)
        return bool(
            state is not None
            and state.status
            in {
                CustomerFulfillmentStatus.SHIPPED,
                CustomerFulfillmentStatus.DELIVERED,
            }
            and state.automation_status == "human_owned"
        )

    def save_handoff_event(
        self,
        *,
        conversation_key: str,
        reason: str,
        created_at: datetime,
        handoff_type: str = "general",
        release_on_resolve: bool = True,
    ) -> None:
        """Persist one open in-app notification per conversation."""
        if handoff_type not in {"general", "post_delivery"}:
            raise ValueError("转人工通知类型无效。")
        expires_at = _timestamp(created_at + timedelta(days=15))
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM cs_handoff_events
                WHERE conversation_key = ?
                  AND (
                    status = 'open'
                    OR (? = 'post_delivery' AND handoff_type = 'post_delivery')
                  )
                ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (conversation_key, handoff_type),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO cs_handoff_events (
                        conversation_key, reason, status, handoff_type,
                        release_on_resolve, created_at, content_expires_at
                    ) VALUES (?, ?, 'open', ?, ?, ?, ?)
                    """,
                    (
                        conversation_key,
                        reason,
                        handoff_type,
                        int(release_on_resolve),
                        _timestamp(created_at),
                        expires_at,
                    ),
                )
                handoff_id = int(cursor.lastrowid)
            else:
                handoff_id = int(existing["id"])
                if handoff_type == "post_delivery":
                    connection.execute(
                        """
                        UPDATE cs_handoff_events
                        SET reason = ?, status = 'open', created_at = ?,
                            content_expires_at = ?
                        WHERE id = ?
                        """,
                        (reason, _timestamp(created_at), expires_at, handoff_id),
                    )
            now = _timestamp(created_at)
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            customer_key = (
                str(mapping["customer_key"])
                if mapping is not None
                else conversation_key
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customers (
                    customer_key, primary_conversation_key,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (customer_key, conversation_key, now, now, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_conversations (
                    conversation_key, customer_key, linked_at, last_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (conversation_key, customer_key, now, now),
            )
            lifecycle_row = connection.execute(
                "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                (customer_key,),
            ).fetchone()
            lifecycle = (
                str(lifecycle_row["lifecycle"])
                if lifecycle_row is not None
                else CustomerLifecycle.NEW.value
            )
            if release_on_resolve:
                connection.execute(
                    """
                    UPDATE cs_customers
                    SET automation_status = 'human_owned', next_follow_up_at = NULL,
                        last_seen_at = ?, updated_at = ?
                    WHERE customer_key = ?
                    """,
                    (now, now, customer_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE cs_customers
                    SET next_follow_up_at = NULL, last_seen_at = ?, updated_at = ?
                    WHERE customer_key = ?
                    """,
                    (now, now, customer_key),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_customer_state_events (
                    event_key, customer_key, conversation_key, event_type,
                    from_lifecycle, to_lifecycle, payload_json, created_at
                ) VALUES (?, ?, ?, 'handoff_opened', ?, ?, ?, ?)
                """,
                (
                    f"handoff:{handoff_id}:opened",
                    customer_key,
                    conversation_key,
                    lifecycle,
                    lifecycle,
                    json.dumps({"reason": reason}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

    def save_post_delivery_handoff(
        self, *, conversation_key: str, reason: str, created_at: datetime
    ) -> None:
        """Create a notification which does not release the delivery lock when closed."""
        self.save_handoff_event(
            conversation_key=conversation_key,
            reason=reason,
            created_at=created_at,
            handoff_type="post_delivery",
            release_on_resolve=False,
        )

    def has_open_handoff(self, conversation_key: str) -> bool:
        """Check whether this conversation is isolated for human handling."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM cs_handoff_events
                WHERE conversation_key = ? AND status = 'open'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone()
        return row is not None

    def list_open_handoff_events(self, limit: int = 100) -> list[HandoffEvent]:
        """Return pending in-app notifications, newest first."""
        if limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_key, reason, status, handoff_type,
                       release_on_resolve, created_at
                FROM cs_handoff_events
                WHERE status = 'open'
                   OR (
                        handoff_type = 'post_delivery'
                        AND EXISTS (
                            SELECT 1 FROM cs_conversation_fulfillment AS fulfillment
                            WHERE fulfillment.conversation_key = cs_handoff_events.conversation_key
                              AND fulfillment.status IN ('shipped', 'delivered')
                              AND fulfillment.automation_status = 'human_owned'
                        )
                   )
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            HandoffEvent(
                event_id=int(row["id"]),
                conversation_key=str(row["conversation_key"]),
                reason=str(row["reason"]),
                status=str(row["status"]),
                created_at=_parse_or_now(str(row["created_at"])),
                handoff_type=str(row["handoff_type"]),
                release_on_resolve=bool(row["release_on_resolve"]),
            )
            for row in rows
        ]

    def resolve_handoff_event(self, event_id: int) -> bool:
        """Release a conversation only after the notification is handled."""
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT conversation_key, release_on_resolve FROM cs_handoff_events
                WHERE id = ? AND status = 'open'
                """,
                (event_id,),
            ).fetchone()
            if existing is None:
                return False
            cursor = connection.execute(
                """
                UPDATE cs_handoff_events
                SET status = 'resolved'
                WHERE id = ? AND status = 'open'
                """,
                (event_id,),
            )
            conversation_key = str(existing["conversation_key"])
            release_on_resolve = bool(existing["release_on_resolve"])
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            if mapping is not None and release_on_resolve:
                customer_key = str(mapping["customer_key"])
                lifecycle_row = connection.execute(
                    "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                    (customer_key,),
                ).fetchone()
                if lifecycle_row is not None:
                    lifecycle = str(lifecycle_row["lifecycle"])
                    now = _timestamp()
                    connection.execute(
                        """
                        UPDATE cs_customers
                        SET automation_status = 'active', updated_at = ?
                        WHERE customer_key = ?
                        """,
                        (now, customer_key),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO cs_customer_state_events (
                            event_key, customer_key, conversation_key, event_type,
                            from_lifecycle, to_lifecycle, payload_json, created_at
                        ) VALUES (?, ?, ?, 'handoff_resolved', ?, ?, '{}', ?)
                        """,
                        (
                            f"handoff:{event_id}:resolved",
                            customer_key,
                            conversation_key,
                            lifecycle,
                            lifecycle,
                            now,
                        ),
                    )
        return cursor.rowcount == 1

    def restore_conversation_automation(
        self, conversation_key: str, *, restored_at: datetime
    ) -> bool:
        """Explicitly release a post-shipment lock for exactly one conversation."""
        now = _timestamp(restored_at)
        with self._connection() as connection:
            current = connection.execute(
                """
                SELECT status, automation_status
                FROM cs_conversation_fulfillment
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            if (
                current is None
                or str(current["status"])
                not in {
                    CustomerFulfillmentStatus.SHIPPED.value,
                    CustomerFulfillmentStatus.DELIVERED.value,
                }
                or str(current["automation_status"]) != "human_owned"
            ):
                return False
            connection.execute(
                """
                UPDATE cs_conversation_fulfillment
                SET automation_status = 'active', updated_at = ?
                WHERE conversation_key = ?
                """,
                (now, conversation_key),
            )
            connection.execute(
                """
                UPDATE cs_conversation_states
                SET human_owned = 0, updated_at = ?
                WHERE conversation_key = ?
                """,
                (now, conversation_key),
            )
            connection.execute(
                """
                UPDATE cs_handoff_events
                SET status = 'resolved'
                WHERE conversation_key = ?
                  AND handoff_type = 'post_delivery'
                  AND status = 'open'
                """,
                (conversation_key,),
            )
            mapping = connection.execute(
                """
                SELECT customer_key FROM cs_customer_conversations
                WHERE conversation_key = ?
                """,
                (conversation_key,),
            ).fetchone()
            if mapping is not None:
                customer_key = str(mapping["customer_key"])
                lifecycle_row = connection.execute(
                    "SELECT lifecycle FROM cs_customers WHERE customer_key = ?",
                    (customer_key,),
                ).fetchone()
                if lifecycle_row is not None:
                    lifecycle = str(lifecycle_row["lifecycle"])
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO cs_customer_state_events (
                            event_key, customer_key, conversation_key, event_type,
                            from_lifecycle, to_lifecycle, payload_json, created_at
                        ) VALUES (?, ?, ?, 'post_delivery_automation_restored',
                                  ?, ?, '{}', ?)
                        """,
                        (
                            f"delivery:{conversation_key}:restored:{now}",
                            customer_key,
                            conversation_key,
                            lifecycle,
                            lifecycle,
                            now,
                        ),
                    )
        return True

    def has_processed_fingerprint(self, batch_fingerprint: str) -> bool:
        """Check the permanent duplicate-send guard."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM cs_processed_fingerprints WHERE batch_fingerprint = ?",
                (batch_fingerprint,),
            ).fetchone()
        return row is not None

    def add_processed_fingerprint(self, batch_fingerprint: str, observed_at: datetime) -> None:
        """Persist a fingerprint without retaining the source message body."""
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO cs_processed_fingerprints (batch_fingerprint, observed_at) VALUES (?, ?)",
                (batch_fingerprint, observed_at.astimezone().isoformat(timespec="seconds")),
            )

    def save_human_confirmed_example(self, example: HistoricalExample) -> None:
        """Persist a manually approved, already-redacted Q&A for future style retrieval."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO cs_training_examples (
                    example_id, customer_text, merchant_text, trust_level,
                    customer_fingerprint, merchant_fingerprint, created_at
                ) VALUES (?, ?, ?, 'human_confirmed', ?, ?, ?)
                """,
                (
                    example.example_id,
                    example.customer_text,
                    example.merchant_text,
                    _fingerprint(example.customer_text),
                    _fingerprint(example.merchant_text),
                    _timestamp(),
                ),
            )

    def save_curated_example(self, example: HistoricalExample) -> None:
        """Upsert one operator-confirmed factual Q&A at the highest trust tier."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_training_examples (
                    example_id, customer_text, merchant_text, trust_level,
                    customer_fingerprint, merchant_fingerprint, created_at
                ) VALUES (?, ?, ?, 'user_curated', ?, ?, ?)
                ON CONFLICT(example_id) DO UPDATE SET
                    customer_text = excluded.customer_text,
                    merchant_text = excluded.merchant_text,
                    trust_level = 'user_curated',
                    customer_fingerprint = excluded.customer_fingerprint,
                    merchant_fingerprint = excluded.merchant_fingerprint
                """,
                (
                    example.example_id,
                    example.customer_text,
                    example.merchant_text,
                    _fingerprint(example.customer_text),
                    _fingerprint(example.merchant_text),
                    _timestamp(),
                ),
            )

    def archive_conflicting_catalog_examples(
        self, current_example_ids: Sequence[str]
    ) -> int:
        """Retain but remove obsolete current-product facts from retrieval."""
        ids = tuple(dict.fromkeys(item for item in current_example_ids if item.strip()))
        placeholders = ",".join("?" for _ in ids) or "''"
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE cs_training_examples
                SET trust_level = 'superseded_catalog'
                WHERE trust_level IN ('user_curated', 'conversation_knowledge', 'cleaned_history')
                  AND example_id NOT IN ({placeholders})
                  AND (
                    ((merchant_text LIKE '%6020%' OR merchant_text LIKE '%60V20%')
                        AND merchant_text LIKE '%428%')
                    OR ((merchant_text LIKE '%6030%' OR merchant_text LIKE '%60V30%')
                        AND (merchant_text LIKE '%658%' OR merchant_text LIKE '%638%'))
                    OR ((merchant_text LIKE '%4830%' OR merchant_text LIKE '%48V30%')
                        AND merchant_text LIKE '%558%')
                    OR (merchant_text LIKE '%4830%' AND merchant_text LIKE '%17-18-32%')
                    OR merchant_text LIKE '%健康度100%'
                    OR ((customer_text LIKE '%蓝牙%' OR merchant_text LIKE '%蓝牙%')
                        AND (merchant_text LIKE '%40元%' OR merchant_text LIKE '%补40%'))
                  )
                """,
                ids,
            )
        return cursor.rowcount

    def cleanup_expired_audit(
        self,
        *,
        now: datetime | None = None,
        retention: timedelta = timedelta(days=15),
        batch_size: int = 500,
    ) -> int:
        """Delete expired audit content in bounded transactions.

        Product knowledge, imported examples, import hashes, and permanent
        fingerprints are intentionally not touched.
        """
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0。")
        current = now or datetime.now().astimezone()
        cutoff = (current - retention).isoformat(timespec="seconds")
        deleted = 0
        while True:
            with self._connection() as connection:
                ids = connection.execute(
                    "SELECT id FROM cs_messages WHERE created_at < ? ORDER BY id LIMIT ?",
                    (cutoff, batch_size),
                ).fetchall()
                if not ids:
                    break
                connection.executemany(
                    "DELETE FROM cs_messages WHERE id = ?",
                    [(int(row["id"]),) for row in ids],
                )
                deleted += len(ids)
        for table, key_column, expiry_column in (
            ("cs_reply_jobs", "job_id", "updated_at"),
            ("cs_price_change_tasks", "task_id", "updated_at"),
            ("cs_handoff_events", "id", "content_expires_at"),
            ("cs_run_sessions", "id", "content_expires_at"),
            ("cs_negotiation_states", "conversation_key", "updated_at"),
            ("cs_sales_states", "conversation_key", "updated_at"),
            ("cs_customer_turns", "turn_id", "updated_at"),
            ("cs_send_outbox", "send_id", "updated_at"),
            ("cs_conversation_states", "conversation_key", "updated_at"),
        ):
            while True:
                with self._connection() as connection:
                    ids = connection.execute(
                        f"SELECT {key_column} FROM {table} WHERE {expiry_column} < ? "
                        f"ORDER BY {key_column} LIMIT ?",
                        (cutoff, batch_size),
                    ).fetchall()
                    if not ids:
                        break
                    connection.executemany(
                        f"DELETE FROM {table} WHERE {key_column} = ?",
                        [(row[key_column],) for row in ids],
                    )
                    deleted += len(ids)
        while True:
            with self._connection() as connection:
                keys = connection.execute(
                    "SELECT conversation_key FROM cs_conversations WHERE content_expires_at < ? LIMIT ?",
                    (cutoff, batch_size),
                ).fetchall()
                if not keys:
                    break
                connection.executemany(
                    "DELETE FROM cs_conversations WHERE conversation_key = ?",
                    [(str(row["conversation_key"]),) for row in keys],
                )
                deleted += len(keys)
        return deleted

    def cleanup_if_due(
        self,
        *,
        now: datetime | None = None,
        interval: timedelta = timedelta(hours=6),
    ) -> int:
        """Run cleanup at startup or when the persisted six-hour interval expires."""
        current = now or datetime.now().astimezone()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM cs_maintenance WHERE name = 'audit_cleanup'"
            ).fetchone()
        if row is not None and current - _parse_or_now(str(row["value"])) < interval:
            return 0
        deleted = self.cleanup_expired_audit(now=current)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cs_maintenance (name, value) VALUES ('audit_cleanup', ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (current.astimezone().isoformat(timespec="seconds"),),
            )
        return deleted

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def _fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")


def _parse_or_now(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now().astimezone()


def _parse_optional_timestamp(value: object) -> datetime | None:
    return _parse_or_now(str(value)) if value is not None else None


def _advanced_customer_lifecycle(
    current: CustomerLifecycle,
    candidate: CustomerLifecycle,
) -> CustomerLifecycle:
    """Advance normal sales states while allowing a lost customer to re-engage."""
    if candidate in {CustomerLifecycle.AFTER_SALES, CustomerLifecycle.LOST}:
        return candidate
    if current is CustomerLifecycle.AFTER_SALES:
        return current
    if current is CustomerLifecycle.LOST:
        return candidate
    rank = {
        CustomerLifecycle.NEW: 0,
        CustomerLifecycle.ENGAGED: 1,
        CustomerLifecycle.QUALIFIED: 2,
        CustomerLifecycle.NEGOTIATING: 3,
        CustomerLifecycle.READY_TO_ORDER: 4,
        CustomerLifecycle.ORDERED: 5,
    }
    return candidate if rank.get(candidate, 0) >= rank.get(current, 0) else current


def _stronger_customer_intent(
    current: CustomerIntentLevel,
    candidate: CustomerIntentLevel,
) -> CustomerIntentLevel:
    rank = {
        CustomerIntentLevel.UNKNOWN: 0,
        CustomerIntentLevel.LOW: 1,
        CustomerIntentLevel.MEDIUM: 2,
        CustomerIntentLevel.HIGH: 3,
    }
    return candidate if rank[candidate] >= rank[current] else current


def _customer_profile_from_row(row: sqlite3.Row) -> CustomerProfile:
    raw_tags = json.loads(str(row["tags_json"]))
    tags = tuple(str(item) for item in raw_tags) if isinstance(raw_tags, list) else ()
    return CustomerProfile(
        customer_key=str(row["customer_key"]),
        platform_customer_id=(
            str(row["platform_customer_id"])
            if row["platform_customer_id"] is not None
            else None
        ),
        primary_conversation_key=str(row["primary_conversation_key"]),
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        lifecycle=CustomerLifecycle(str(row["lifecycle"])),
        intent_level=CustomerIntentLevel(str(row["intent_level"])),
        sales_stage=(
            SalesStage(str(row["sales_stage"]))
            if row["sales_stage"] is not None
            else None
        ),
        order_status=CustomerOrderStatus(str(row["order_status"])),
        fulfillment_status=CustomerFulfillmentStatus(str(row["fulfillment_status"])),
        shipped_at=_parse_optional_timestamp(row["shipped_at"]),
        delivered_at=_parse_optional_timestamp(row["delivered_at"]),
        automation_status=str(row["automation_status"]),
        current_product_key=(
            str(row["current_product_key"])
            if row["current_product_key"] is not None
            else None
        ),
        platform_product_id=(
            str(row["platform_product_id"])
            if row["platform_product_id"] is not None
            else None
        ),
        battery_model=str(row["battery_model"]) if row["battery_model"] is not None else None,
        required_range_km=(
            str(row["required_range_km"])
            if row["required_range_km"] is not None
            else None
        ),
        motor_power_w=(
            str(row["motor_power_w"]) if row["motor_power_w"] is not None else None
        ),
        quantity=int(row["quantity"]),
        budget_amount=(
            str(row["budget_amount"]) if row["budget_amount"] is not None else None
        ),
        last_customer_offer=(
            str(row["last_customer_offer"])
            if row["last_customer_offer"] is not None
            else None
        ),
        accepted_price=(
            str(row["accepted_price"]) if row["accepted_price"] is not None else None
        ),
        tags=tags,
        notes=str(row["notes"]),
        first_seen_at=_parse_optional_timestamp(row["first_seen_at"]),
        last_seen_at=_parse_optional_timestamp(row["last_seen_at"]),
        last_customer_message_at=_parse_optional_timestamp(row["last_customer_message_at"]),
        last_merchant_message_at=_parse_optional_timestamp(row["last_merchant_message_at"]),
        next_follow_up_at=_parse_optional_timestamp(row["next_follow_up_at"]),
        conversation_count=int(row["conversation_count"]),
        created_at=_parse_optional_timestamp(row["created_at"]),
        updated_at=_parse_optional_timestamp(row["updated_at"]),
    )


def _fulfillment_state_from_row(row: sqlite3.Row) -> ConversationFulfillmentState:
    return ConversationFulfillmentState(
        conversation_key=str(row["conversation_key"]),
        platform_product_id=(
            str(row["platform_product_id"])
            if row["platform_product_id"] is not None
            else None
        ),
        status=CustomerFulfillmentStatus(str(row["status"])),
        evidence_key=(
            str(row["evidence_key"]) if row["evidence_key"] is not None else None
        ),
        evidence_text=(
            str(row["evidence_text"]) if row["evidence_text"] is not None else None
        ),
        shipped_at=_parse_optional_timestamp(row["shipped_at"]),
        delivered_at=_parse_optional_timestamp(row["delivered_at"]),
        automation_status=str(row["automation_status"]),
        updated_at=_parse_optional_timestamp(row["updated_at"]),
    )

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
    ConversationSnapshot,
    ConversationSummary,
    HandoffEvent,
    HistoricalExample,
    MediaAsset,
    PriceChangeDraft,
    PriceChangeStatus,
    ProductKnowledge,
    ReplyDraft,
    ReplyJobStatus,
)
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
                    content_expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cs_processed_fingerprints (
                    batch_fingerprint TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL
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

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        """Read a non-secret application setting."""
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM cs_settings WHERE name = ?", (name,)).fetchone()
        return default if row is None else str(row["value"])

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

    def save_handoff_event(
        self, *, conversation_key: str, reason: str, created_at: datetime
    ) -> None:
        """Persist one open in-app notification per conversation."""
        expires_at = _timestamp(created_at + timedelta(days=15))
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM cs_handoff_events
                WHERE conversation_key = ? AND status = 'open'
                LIMIT 1
                """,
                (conversation_key,),
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                """
                INSERT INTO cs_handoff_events (
                    conversation_key, reason, status, created_at, content_expires_at
                ) VALUES (?, ?, 'open', ?, ?)
                """,
                (conversation_key, reason, _timestamp(created_at), expires_at),
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
                SELECT id, conversation_key, reason, status, created_at
                FROM cs_handoff_events
                WHERE status = 'open'
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
            )
            for row in rows
        ]

    def resolve_handoff_event(self, event_id: int) -> bool:
        """Release a conversation only after the notification is handled."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE cs_handoff_events
                SET status = 'resolved'
                WHERE id = ? AND status = 'open'
                """,
                (event_id,),
            )
        return cursor.rowcount == 1

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

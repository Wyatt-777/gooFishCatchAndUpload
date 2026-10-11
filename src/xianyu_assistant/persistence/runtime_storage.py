"""Runtime evidence and durable submission guards, separate from chat content."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any
from uuid import uuid4

UNRESOLVED_SUBMISSION_STATES = ("authorized", "uncertain", "persistence_failed")


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("运行证据时间必须带时区。")
    return value.astimezone().isoformat(timespec="seconds")


def initialize_runtime_storage(connection: sqlite3.Connection) -> None:
    """Add only operational tables/columns; never rewrite product prices."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS cs_checkout_intents (
            send_id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL,
            merchant_account_id TEXT NOT NULL, platform_customer_id TEXT NOT NULL,
            platform_product_id TEXT NOT NULL, product_key TEXT NOT NULL,
            approved_price TEXT NOT NULL, price_source TEXT NOT NULL,
            price_variant TEXT NOT NULL, quantity INTEGER NOT NULL,
            settings_version TEXT NOT NULL, reply_fingerprint TEXT NOT NULL,
            customer_message_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cs_checkout_intents_conversation
            ON cs_checkout_intents(conversation_key,created_at);
        CREATE TABLE IF NOT EXISTS cs_submission_tombstones (
            action_id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
            conversation_key TEXT NOT NULL, turn_fingerprint TEXT,
            binding_hash TEXT NOT NULL, permission_issued INTEGER NOT NULL DEFAULT 0,
            permit_id TEXT, request_id TEXT, epoch INTEGER, actor_id TEXT,
            state TEXT NOT NULL, failure_code TEXT, run_id INTEGER,
            outgoing_message_key TEXT, evidence_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cs_submission_conversation
            ON cs_submission_tombstones(conversation_key,state);
        CREATE INDEX IF NOT EXISTS idx_cs_submission_turn
            ON cs_submission_tombstones(conversation_key,action_type,turn_fingerprint);
        CREATE TABLE IF NOT EXISTS cs_runtime_configs (
            run_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cs_browser_operations (
            request_id TEXT PRIMARY KEY, run_id INTEGER, epoch INTEGER,
            operation TEXT NOT NULL, conversation_key TEXT, action_id TEXT,
            started_at TEXT NOT NULL, completed_at TEXT, deadline_seconds REAL,
            outcome TEXT NOT NULL, diagnostic_json TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cs_browser_operations_run
            ON cs_browser_operations(run_id,started_at);
        CREATE TABLE IF NOT EXISTS cs_runtime_recoveries (
            event_id TEXT PRIMARY KEY, run_id INTEGER, epoch INTEGER,
            failure_code TEXT NOT NULL, recovery_state TEXT NOT NULL,
            conversation_key TEXT, action_id TEXT, diagnostic_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cs_media_cache (
            account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
            message_key TEXT NOT NULL, media_kind TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL, parser_version TEXT NOT NULL,
            status TEXT NOT NULL, result_text TEXT, evidence_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(account_key,conversation_key,message_key,media_kind,
                        content_fingerprint,parser_version)
        );
    """)
    for table in ("cs_reply_jobs", "cs_price_change_tasks", "cs_run_sessions"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name in ("failure_code", "recovery_state"):
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(cs_sales_states)")}
    for name, definition in (
        ("pre_submit_retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("defer_reason", "TEXT"), ("follow_up_expires_at", "TEXT"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE cs_sales_states ADD COLUMN {name} {definition}")


def binding_hash(action_type: str, conversation_key: str, evidence: dict[str, Any]) -> str:
    fields = ("merchant_account_id", "platform_customer_id", "product_id", "order_id")
    binding: dict[str, Any] = {
        "action_type": action_type, "conversation_key": conversation_key,
        **{field: evidence.get(field) for field in fields},
    }
    if action_type == "price":
        binding.update(approved_price=evidence.get("approved_price"),
                       shipping_price=evidence.get("shipping_price"))
        if evidence.get("checkout_send_id"):
            binding["checkout_send_id"] = evidence["checkout_send_id"]
        if not evidence.get("order_id"):
            binding.update(order_card_message_key=evidence.get("order_card_message_key"),
                           order_card_key_source=evidence.get("order_card_key_source"))
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class RuntimeStorage:
    """Mixin for CustomerServiceRepository; all writes use its bounded connection."""

    def save_checkout_intent(self, *, send_id: str, snapshot: Any, product_key: str,
                             approved_price: str, price_source: str, price_variant: str,
                             settings_version: str, reply_fingerprint: str,
                             updated_at: datetime) -> bool:
        from xianyu_assistant.customer_service.price_change import format_money, parse_money

        if price_source not in {"configured_price", "accepted_negotiation"}:
            raise ValueError("下单成交价来源必须为客服设置或本地已确认讲价。")
        if not all((snapshot.merchant_account_id, snapshot.platform_customer_id,
                    snapshot.platform_product_id, snapshot.last_incoming_message,
                    product_key, settings_version, reply_fingerprint)):
            return False  # An unbound commitment cannot later authorize a financial action.
        now = stamp(updated_at)
        with self._connection() as connection:
            connection.execute("""INSERT INTO cs_checkout_intents
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,'prepared',?,?)
                ON CONFLICT(send_id) DO UPDATE SET approved_price=excluded.approved_price,
                price_source=excluded.price_source,price_variant=excluded.price_variant,
                product_key=excluded.product_key,settings_version=excluded.settings_version,
                reply_fingerprint=excluded.reply_fingerprint,updated_at=excluded.updated_at
                WHERE cs_checkout_intents.state='prepared' AND NOT EXISTS
                (SELECT 1 FROM cs_quality_sends WHERE send_id=excluded.send_id AND status='verified')""",
                (send_id, snapshot.conversation_key, snapshot.merchant_account_id,
                 snapshot.platform_customer_id, snapshot.platform_product_id, product_key,
                 format_money(parse_money(approved_price)), price_source, price_variant,
                 settings_version, reply_fingerprint, snapshot.last_incoming_message.message_key, now, now))
        return True

    def load_checkout_intent(self, conversation_key: str, *, now: datetime) -> dict[str, Any] | None:
        """Activate only page-verified sends; a restart needs no in-memory callback."""
        from datetime import timedelta

        from xianyu_assistant.customer_service.fingerprints import content_fingerprint

        with self._connection() as connection:
            # Select the latest verified commitment, including mismatches. Never fall
            # back to an older price after a newer send was edited or invalidated.
            row = connection.execute("""SELECT i.*,s.final_text,s.updated_at AS sent_at FROM cs_checkout_intents i
                JOIN cs_quality_sends s ON s.send_id=i.send_id
                WHERE i.conversation_key=? AND s.status='verified'
                AND s.origin IN ('application_auto','application_reviewed') AND i.created_at>=?
                ORDER BY i.created_at DESC,i.rowid DESC LIMIT 1""",
                (conversation_key, stamp(now - timedelta(days=7)))).fetchone()
            external = (connection.execute("""SELECT text FROM cs_quality_messages
                WHERE conversation_key=? AND direction='outgoing' AND observed_at>?
                AND source IN ('merchant_external','merchant_source_uncertain')""",
                (conversation_key, row["sent_at"])).fetchall() if row is not None else ())
        if row is None:
            return None
        result = dict(row)
        if result["state"] != "prepared" or content_fingerprint(result.pop("final_text") or "") != result["reply_fingerprint"]:
            return None
        from xianyu_assistant.customer_service.contact_policy import is_order_card

        if any(not is_order_card(item["text"] or "") for item in external):
            return None  # A human/external merchant intervention invalidates old consent.
        return result

    def invalidate_checkout_intents(self, conversation_key: str, *, updated_at: datetime) -> None:
        with self._connection() as connection:
            connection.execute("""UPDATE cs_checkout_intents SET state='superseded',updated_at=?
                WHERE conversation_key=? AND state='prepared'""", (stamp(updated_at), conversation_key))

    def save_run_runtime_config(self, run_id: int, *, settings: dict[str, Any],
                                recorded_at: datetime) -> None:
        from xianyu_assistant.customer_service.send_reliability import safe_error

        # Configuration is supplied by the actual executor/model configuration.
        # Do not retain secrets even when a caller mistakenly includes one.
        def sanitized(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: ("[REDACTED]" if any(word in key.lower() for word in (
                    "password", "api_key", "access_token", "authorization", "credential",
                )) else sanitized(item)) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [sanitized(item) for item in value]
            return safe_error(value) if isinstance(value, str) else value

        now = stamp(recorded_at)
        with self._connection() as connection:
            connection.execute("""INSERT INTO cs_runtime_configs VALUES (?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET settings_json=excluded.settings_json,
                updated_at=excluded.updated_at""",
                (run_id, json.dumps(sanitized(settings), ensure_ascii=False), now, now))

    def record_browser_operation(
        self, *, request_id: str, operation: str, started_at: datetime,
        outcome: str, run_id: int | None = None, epoch: int | None = None,
        completed_at: datetime | None = None, deadline_seconds: float | None = None,
        conversation_key: str | None = None, action_id: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        now = stamp(completed_at or started_at)
        with self._connection() as connection:
            connection.execute("""INSERT INTO cs_browser_operations
                (request_id,run_id,epoch,operation,conversation_key,action_id,started_at,
                 completed_at,deadline_seconds,outcome,diagnostic_json,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
                completed_at=excluded.completed_at,outcome=excluded.outcome,
                diagnostic_json=excluded.diagnostic_json,updated_at=excluded.updated_at""",
                (request_id, run_id, epoch, operation, conversation_key, action_id,
                 stamp(started_at), stamp(completed_at) if completed_at else None,
                 deadline_seconds, outcome, json.dumps(diagnostic or {}, ensure_ascii=False), now))

    def record_runtime_recovery(
        self, *, failure_code: str, recovery_state: str, created_at: datetime,
        run_id: int | None = None, epoch: int | None = None,
        conversation_key: str | None = None, action_id: str | None = None,
        diagnostic: dict[str, Any] | None = None, event_id: str | None = None,
    ) -> str:
        identifier = event_id or uuid4().hex
        with self._connection() as connection:
            connection.execute("""INSERT OR IGNORE INTO cs_runtime_recoveries
                VALUES (?,?,?,?,?,?,?,?,?)""", (identifier, run_id, epoch, failure_code,
                recovery_state, conversation_key, action_id,
                json.dumps(diagnostic or {}, ensure_ascii=False), stamp(created_at)))
        return identifier

    def get_submission(self, action_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM cs_submission_tombstones WHERE action_id=?",
                                     (action_id,)).fetchone()
        return dict(row) if row is not None else None

    def has_unresolved_submission(self, conversation_key: str) -> bool:
        with self._connection() as connection:
            return connection.execute("""SELECT 1 FROM cs_submission_tombstones
                WHERE conversation_key=? AND permission_issued=1
                AND state IN ('authorized','uncertain','persistence_failed')
                LIMIT 1""", (conversation_key,)).fetchone() is not None

    def prepare_submission(self, *, action_id: str, action_type: str, conversation_key: str,
                           updated_at: datetime, turn_fingerprint: str | None = None,
                           run_id: int | None = None) -> None:
        """Mark a new-protocol operation before any browser call, without a permit."""
        now = stamp(updated_at)
        with self._connection() as connection:
            connection.execute("""INSERT OR IGNORE INTO cs_submission_tombstones
                (action_id,action_type,conversation_key,turn_fingerprint,binding_hash,
                 permission_issued,state,run_id,created_at,updated_at)
                VALUES (?,?,?,?,?,0,'prepared',?,?,?)""", (action_id, action_type,
                conversation_key, turn_fingerprint, binding_hash(action_type, conversation_key, {}),
                run_id, now, now))

    def authorize_submission(
        self, *, action_id: str, action_type: str, conversation_key: str,
        evidence: dict[str, Any], updated_at: datetime, run_id: int | None = None,
        turn_fingerprint: str | None = None, attempt_id: str | None = None,
        permit_id: str | None = None,
    ) -> str:
        """Persist single-use permission before the parent can acknowledge a click."""
        if action_type not in {"text", "price"} or not action_id or not conversation_key:
            raise ValueError("提交操作类型或标识无效。")
        if evidence.get("conversation_key") != conversation_key:
            raise ValueError("提交证据与会话身份不一致。")
        if action_type == "price" and not all(evidence.get(key) for key in (
            "merchant_account_id", "platform_customer_id", "product_id",
            "approved_price", "shipping_price", "last_customer_key", "submitted_at",
        )):
            raise ValueError("改价最终确认缺少稳定订单、会话或金额证据。")
        if action_type == "price":
            from xianyu_assistant.customer_service.checkout import reliable_order_binding

            if not reliable_order_binding(evidence):
                raise ValueError("改价缺少订单编号或稳定的待付款订单卡标识。")
        if action_type == "price" and evidence.get("order_price_verifiable") is not True:
            raise ValueError("实际订单金额及运费不可回读，禁止发出最终提交许可。")
        if action_type == "price" and turn_fingerprint is None:
            turn_fingerprint = str(evidence["last_customer_key"])
        now = stamp(updated_at)
        identifier = permit_id or uuid4().hex
        identity = binding_hash(action_type, conversation_key, evidence)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM cs_submission_tombstones WHERE action_id=?",
                                     (action_id,)).fetchone()
            if old is not None and (old["permission_issued"]
                                    or old["state"] not in {"prepared", "not_performed", "persistence_failed"}):
                raise ValueError("该操作已有提交许可或完成凭据，禁止重提。")
            if old is not None and (old["action_type"] != action_type
                                    or old["conversation_key"] != conversation_key):
                raise ValueError("不能改变已有操作的类型或会话。")
            conflict = connection.execute("""SELECT 1 FROM cs_submission_tombstones
                WHERE conversation_key=? AND action_id<>? AND
                (permission_issued=1 AND (state IN ('authorized','uncertain','persistence_failed') OR
                 (action_type=? AND state<>'not_performed' AND
                  ((? IS NOT NULL AND turn_fingerprint=?) OR
                   (?='price' AND binding_hash=?) OR
                   (?='price' AND ? IS NOT NULL AND json_extract(
                       CASE WHEN json_valid(evidence_json) THEN evidence_json ELSE '{}' END,
                       '$.checkout_send_id')=?))))) LIMIT 1""",
                (conversation_key, action_id, action_type, turn_fingerprint,
                 turn_fingerprint, action_type, identity, action_type,
                 evidence.get("checkout_send_id"), evidence.get("checkout_send_id"))).fetchone()
            if conflict is not None:
                raise ValueError("会话已有未决提交，或当前轮次/订单金额已提交，禁止重复操作。")
            connection.execute("""INSERT INTO cs_submission_tombstones
                (action_id,action_type,conversation_key,turn_fingerprint,binding_hash,
                 permission_issued,permit_id,request_id,epoch,actor_id,state,run_id,
                 evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,1,?,?,?,?,'authorized',?,?,?,?)
                ON CONFLICT(action_id) DO UPDATE SET binding_hash=excluded.binding_hash,
                turn_fingerprint=COALESCE(excluded.turn_fingerprint,cs_submission_tombstones.turn_fingerprint),
                permission_issued=1,permit_id=excluded.permit_id,request_id=excluded.request_id,
                epoch=excluded.epoch,actor_id=excluded.actor_id,state='authorized',failure_code=NULL,
                run_id=excluded.run_id,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (action_id, action_type, conversation_key, turn_fingerprint, identity, identifier,
                 evidence.get("request_id"), evidence.get("epoch"), evidence.get("actor_id"),
                 run_id, json.dumps(evidence, ensure_ascii=False), now, now))
            if action_type == "text":
                connection.execute("""UPDATE cs_quality_sends SET status='sending',stage='submitting',
                    click_state='may_have_submitted',evidence_json=?,updated_at=? WHERE send_id=?""",
                    (json.dumps(evidence, ensure_ascii=False), now, action_id))
                connection.execute("UPDATE cs_send_outbox SET status='sending',updated_at=? WHERE send_id=?",
                                   (now, action_id))
                if attempt_id:
                    connection.execute("""UPDATE cs_send_attempts SET stage='submitting',
                        click_state='may_have_submitted',evidence_json=?,updated_at=? WHERE attempt_id=?""",
                        (json.dumps(evidence, ensure_ascii=False), now, attempt_id))
        return identifier

    def finish_submission(
        self, action_id: str, *, state: str, updated_at: datetime,
        evidence: dict[str, Any] | None = None, failure_code: str | None = None,
        outgoing_message_key: str | None = None, proved_not_performed: bool = False,
    ) -> None:
        if state not in {"verified", "uncertain", "persistence_failed", "not_performed"}:
            raise ValueError("提交结束状态无效。")
        if state == "not_performed" and not proved_not_performed:
            raise ValueError("没有明确未点击证据，不能释放提交锁。")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if state == "not_performed":
                row = connection.execute("SELECT permission_issued FROM cs_submission_tombstones WHERE action_id=?",
                                         (action_id,)).fetchone()
                if row is not None and row["permission_issued"]:
                    raise ValueError("已发出提交许可，不能改为未执行或释放重试。")
            connection.execute("""UPDATE cs_submission_tombstones SET state=?,failure_code=?,
                evidence_json=COALESCE(?,evidence_json),outgoing_message_key=COALESCE(?,outgoing_message_key),
                updated_at=? WHERE action_id=? AND state<>'verified'""",
                (state, failure_code, json.dumps(evidence, ensure_ascii=False)
                 if evidence is not None else None, outgoing_message_key, stamp(updated_at), action_id))
            if state == "verified":
                connection.execute("""UPDATE cs_price_change_tasks SET status='applied',
                    failure_code=NULL,recovery_state='recovered',failure_reason=NULL,updated_at=?
                    WHERE task_id=? AND EXISTS (SELECT 1 FROM cs_submission_tombstones
                        WHERE action_id=? AND action_type='price' AND state='verified')""",
                    (stamp(updated_at), action_id, action_id))

    def restore_submission_guard(
        self, *, action_id: str, action_type: str, conversation_key: str,
        evidence: dict[str, Any], updated_at: datetime,
        turn_fingerprint: str | None = None, run_id: int | None = None,
    ) -> None:
        """Import a journal conservatively; never create another permission."""
        now = stamp(updated_at)
        with self._connection() as connection:
            connection.execute("""INSERT INTO cs_submission_tombstones
                (action_id,action_type,conversation_key,turn_fingerprint,binding_hash,permission_issued,
                 state,failure_code,run_id,evidence_json,created_at,updated_at)
                VALUES (?,?,?,?,?,1,'uncertain','interrupted_submission',?,?,?,?)
                ON CONFLICT(action_id) DO UPDATE SET permission_issued=CASE
                    WHEN cs_submission_tombstones.state='not_performed' AND
                         cs_submission_tombstones.permission_issued=0 THEN 0 ELSE 1 END,
                state=CASE WHEN cs_submission_tombstones.state IN ('verified','manual_resolved','not_performed')
                    THEN cs_submission_tombstones.state ELSE 'uncertain' END,
                evidence_json=COALESCE(cs_submission_tombstones.evidence_json,excluded.evidence_json),
                updated_at=excluded.updated_at""", (action_id, action_type, conversation_key,
                turn_fingerprint, binding_hash(action_type, conversation_key, evidence), run_id,
                json.dumps(evidence, ensure_ascii=False), now, now))

    def save_job_recovery_state(self, job_id: str, *, failure_code: str | None,
                                recovery_state: str | None, updated_at: datetime) -> None:
        with self._connection() as connection:
            connection.execute("""UPDATE cs_reply_jobs SET failure_code=?,recovery_state=?,updated_at=?
                WHERE job_id=?""", (failure_code, recovery_state, stamp(updated_at), job_id))

    def get_reply_job_recovery(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("""SELECT job_id,conversation_key,batch_fingerprint,status,
                failure_code,recovery_state FROM cs_reply_jobs WHERE job_id=?""", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_recovery_pending_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("""SELECT job_id,conversation_key,batch_fingerprint,status,
                failure_code,recovery_state FROM cs_reply_jobs WHERE recovery_state='recovery_pending'
                ORDER BY updated_at LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def resolve_recovery_pending_job(self, job_id: str, *, updated_at: datetime,
                                     recovery_state: str = "recovered") -> None:
        with self._connection() as connection:
            connection.execute("""UPDATE cs_reply_jobs SET recovery_state=?,updated_at=?
                WHERE job_id=? AND recovery_state='recovery_pending'""",
                (recovery_state, stamp(updated_at), job_id))

    def get_media_cache(self, *, account_key: str, conversation_key: str, message_key: str,
                        media_kind: str, content_fingerprint: str, parser_version: str,
                        now: datetime) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("""SELECT * FROM cs_media_cache WHERE account_key=?
                AND conversation_key=? AND message_key=? AND media_kind=? AND content_fingerprint=?
                AND parser_version=? AND expires_at>? AND status='success'""",
                (account_key, conversation_key, message_key, media_kind, content_fingerprint,
                 parser_version, stamp(now))).fetchone()
        return dict(row) if row is not None else None

    def save_media_cache(self, *, account_key: str, conversation_key: str, message_key: str,
                         media_kind: str, content_fingerprint: str, parser_version: str,
                         status: str, expires_at: datetime, updated_at: datetime,
                         result_text: str | None = None, evidence: dict[str, Any] | None = None,
                         attempt_count: int = 1) -> None:
        if media_kind != "voice" or status != "success" or not result_text:
            raise ValueError("持久化媒体缓存仅接受成功的语音转写文本。")
        if not all((account_key, conversation_key, message_key, content_fingerprint, parser_version)):
            raise ValueError("转写缓存必须绑定账号、会话、消息、来源及解析版本。")
        with self._connection() as connection:
            connection.execute("""INSERT INTO cs_media_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_key,conversation_key,message_key,media_kind,content_fingerprint,
                            parser_version) DO UPDATE SET status=excluded.status,
                result_text=excluded.result_text,evidence_json=excluded.evidence_json,
                attempt_count=excluded.attempt_count,expires_at=excluded.expires_at,
                updated_at=excluded.updated_at""", (account_key, conversation_key, message_key,
                media_kind, content_fingerprint, parser_version, status, result_text,
                json.dumps(evidence or {}, ensure_ascii=False), attempt_count,
                stamp(expires_at), stamp(updated_at)))

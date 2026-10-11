"""Evidence-bound welcome windows after fifteen minutes of customer inactivity."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from xianyu_assistant.customer_service.contact_policy import (
    is_order_card,
    order_state_from_text,
    quoted_customer_question,
)
from xianyu_assistant.customer_service.fact_guard import critical_fields
from xianyu_assistant.customer_service.fingerprints import (
    content_fingerprint,
    normalize_for_matching,
)
from xianyu_assistant.customer_service.message_roles import role_is_trusted
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
)
from xianyu_assistant.customer_service.reply_questions import is_model_answer
from xianyu_assistant.customer_service.semantic_analysis import analyze_customer_turn

RECONTACT_POLICY_VERSION = "recontact15-v1"
RECONTACT_SECONDS = 15 * 60
_REOPENABLE_REASONS = {
    None, "welcome_sent", "verified_merchant_reply", "merchant_history", "welcome_observed",
    "historical_welcome_observed", "legacy_merchant_activity", "legacy_first_contact",
    "submission_existing_history",
}


def initialize_recontact_storage(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS cs_contact_activity (
            contact_key TEXT PRIMARY KEY, last_customer_at TEXT NOT NULL,
            last_customer_message_key TEXT NOT NULL, last_question_fingerprint TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cs_contact_seen_messages (
            contact_key TEXT NOT NULL, message_key TEXT NOT NULL, customer_at TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(contact_key,message_key));
        CREATE TABLE IF NOT EXISTS cs_recontact_windows (
            window_id TEXT PRIMARY KEY, contact_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
            turn_id TEXT NOT NULL, source_message_key TEXT NOT NULL, last_customer_message_key TEXT NOT NULL,
            previous_customer_at TEXT NOT NULL, customer_at TEXT NOT NULL, idle_seconds REAL NOT NULL,
            baseline_keys_json TEXT NOT NULL, status TEXT NOT NULL, reservation_id TEXT,
            reply_fingerprint TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_recontact_windows_contact
            ON cs_recontact_windows(contact_key,created_at);
    """)


def _timestamp(message: ChatMessage, observed: datetime) -> datetime:
    # A time without a date/timezone is not evidence of a fifteen-minute gap.
    value = message.platform_time
    if value is None or value.tzinfo is None or value > observed:
        return observed.astimezone(UTC)
    return value.astimezone(UTC)


def _question_fingerprint(text: str) -> str:
    return content_fingerprint(re.sub(r"[\s，,。.!！?？、:：;；]+", "", normalize_for_matching(text)))


def is_recontact_question(text: str) -> bool:
    folded = normalize_for_matching(text).strip(" ?？。!！")
    if (not folded or is_order_card(text) or order_state_from_text(text)
            or is_model_answer(text)
            or folded in {"好", "好的", "嗯", "收到", "谢谢", "谢谢你", "行", "可以", "不用了", "再见"}):
        return False
    semantics = analyze_customer_turn(text)
    return bool(critical_fields(text) or semantics.questions or semantics.is_negotiation
                or re.search(r"[?？]", text)
                or re.search(r"怎么|怎样|如何|为什么|多少|哪[个种家里款]|什么|是否|能不能|有没有|吗$|么$", folded))


def observe_recontact_turn(
    connection: sqlite3.Connection, contact_key: str, snapshot: ConversationSnapshot,
    current: Sequence[ChatMessage], *, turn_id: str, observed_at: datetime,
    templates: tuple[str, ...],
) -> None:
    """Observe arrivals once, never turn rereading a page into customer activity."""
    if (not current or not snapshot.platform_customer_id or not snapshot.merchant_account_id
            or snapshot.platform_customer_id == snapshot.merchant_account_id
            or contact_key != "buyer-" + content_fingerprint(snapshot.merchant_account_id, snapshot.platform_customer_id)
            or snapshot.conversation_key.startswith("dom-")
            or any(m.direction is MessageDirection.UNKNOWN for m in snapshot.messages)
            or any(m.direction is not MessageDirection.INCOMING
                   or not role_is_trusted(m.direction, m.role_source)
                   or m.key_source in {"adapter", "", "content_fallback", "position_fallback"}
                   for m in current)):
        return
    now = observed_at.astimezone(UTC).isoformat()
    fresh = []
    for message in current:
        inserted = connection.execute("""INSERT OR IGNORE INTO cs_contact_seen_messages
            (contact_key,message_key,customer_at,content_fingerprint,observed_at) VALUES (?,?,?,?,?)""",
            (contact_key, message.message_key, _timestamp(message, observed_at).isoformat(),
             content_fingerprint(message.kind.value, message.text or ""), now))
        if inserted.rowcount:
            fresh.append(message)
    # Loading older virtualized history cannot advance the inactivity clock.
    if not fresh or fresh[-1].message_key != current[-1].message_key:
        return
    previous = connection.execute("SELECT * FROM cs_contact_activity WHERE contact_key=?",
                                  (contact_key,)).fetchone()
    first_at, last_at = _timestamp(fresh[0], observed_at), _timestamp(fresh[-1], observed_at)
    if previous and last_at < datetime.fromisoformat(previous["last_customer_at"]):
        return
    window = connection.execute("""SELECT * FROM cs_recontact_windows WHERE contact_key=?
        ORDER BY created_at DESC,rowid DESC LIMIT 1""", (contact_key,)).fetchone()
    keys = {m.message_key for m in current}
    carry = bool(window and window["status"] == "eligible"
                 and window["conversation_key"] == snapshot.conversation_key
                 and window["source_message_key"] in keys)
    question_messages = current if carry else fresh
    question = quoted_customer_question(" ".join(m.text or "" for m in question_messages), templates)
    question_hash = _question_fingerprint(question)
    if carry:
        connection.execute("""UPDATE cs_recontact_windows SET turn_id=?,last_customer_message_key=?,
            updated_at=? WHERE window_id=?""", (turn_id, current[-1].message_key, now, window["window_id"]))
    else:
        connection.execute("""UPDATE cs_recontact_windows SET status='superseded',updated_at=?
            WHERE contact_key=? AND status='eligible'""", (now, contact_key))
        gap = (first_at - datetime.fromisoformat(previous["last_customer_at"])).total_seconds() if previous else 0
        if (previous and gap >= RECONTACT_SECONDS and is_recontact_question(question)
                and question_hash != previous["last_question_fingerprint"]):
            window_id = content_fingerprint(contact_key, fresh[0].message_key)
            connection.execute("""INSERT OR IGNORE INTO cs_recontact_windows
                (window_id,contact_key,conversation_key,turn_id,source_message_key,last_customer_message_key,
                 previous_customer_at,customer_at,idle_seconds,baseline_keys_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'eligible',?,?)""", (window_id, contact_key,
                snapshot.conversation_key, turn_id, fresh[0].message_key, current[-1].message_key,
                previous["last_customer_at"], first_at.isoformat(), gap,
                json.dumps([m.message_key for m in snapshot.messages]), now, now))
    connection.execute("""INSERT INTO cs_contact_activity
        (contact_key,last_customer_at,last_customer_message_key,last_question_fingerprint,updated_at)
        VALUES (?,?,?,?,?) ON CONFLICT(contact_key) DO UPDATE SET
        last_customer_at=excluded.last_customer_at,last_customer_message_key=excluded.last_customer_message_key,
        last_question_fingerprint=excluded.last_question_fingerprint,updated_at=excluded.updated_at""",
        (contact_key, last_at.isoformat(), current[-1].message_key, question_hash, now))


def recontact_decision(connection: sqlite3.Connection, conversation_key: str,
                       contact_key: str, guard: sqlite3.Row | None,
                       reservation_id: str | None) -> dict[str, object] | None:
    window = connection.execute("""SELECT * FROM cs_recontact_windows WHERE contact_key=?
        ORDER BY created_at DESC,rowid DESC LIMIT 1""", (contact_key,)).fetchone()
    if (not window or window["conversation_key"] != conversation_key
            or window["status"] not in {"eligible", "reserved"}):
        return None
    reason = "returning_customer_new_question"
    binding = connection.execute("SELECT identity_source FROM cs_contact_bindings WHERE conversation_key=?",
                                 (conversation_key,)).fetchone()
    if not binding or binding[0] != "merchant_buyer_id":
        reason = "contact_identity_unconfirmed"
    elif not guard or guard["order_state"]:
        reason = "order_observed"
    elif guard["closed_reason"] not in _REOPENABLE_REASONS or guard["welcome_hold_reason"]:
        reason = str(guard["welcome_hold_reason"] or guard["closed_reason"])
    elif guard["welcome_status"] == "reserved":
        reason = "welcome_reserved"
    elif window["status"] == "reserved" and window["reservation_id"] != reservation_id:
        reason = "recontact_reserved"
    elif connection.execute("""SELECT 1 FROM cs_recontact_windows WHERE contact_key=?
        AND status='reserved' AND window_id<>? LIMIT 1""",
        (contact_key, window["window_id"])).fetchone():
        # A crash between reserving and preparing a send must keep the old lock.
        reason = "recontact_reserved"
    elif connection.execute("""SELECT 1 FROM cs_contact_bindings b JOIN cs_conversation_states s
        ON s.conversation_key=b.conversation_key WHERE b.contact_key=? AND s.human_owned=1 LIMIT 1""",
        (contact_key,)).fetchone():
        reason = "human_owned"
    elif connection.execute("""SELECT 1 FROM cs_contact_bindings b JOIN cs_handoff_events h
        ON h.conversation_key=b.conversation_key WHERE b.contact_key=? AND h.status='open' LIMIT 1""",
        (contact_key,)).fetchone():
        reason = "open_handoff"
    elif connection.execute("""SELECT 1 FROM cs_contact_bindings b JOIN cs_quality_sends s
        ON s.conversation_key=b.conversation_key WHERE b.contact_key=?
        AND s.status IN ('prepared','sending','uncertain','verified_unrecorded','persistence_failed')
        AND s.send_id<>? LIMIT 1""", (contact_key, reservation_id or "")).fetchone() or connection.execute("""SELECT 1 FROM cs_contact_bindings b JOIN cs_submission_tombstones s
        ON s.conversation_key=b.conversation_key WHERE b.contact_key=? AND s.permission_issued=1
        AND s.state IN ('authorized','uncertain','persistence_failed') AND s.action_id<>? LIMIT 1""",
        (contact_key, reservation_id or "")).fetchone():
        reason = "submission_pending"
    return {"allowed": reason == "returning_customer_new_question", "reason": reason,
            "recontact": True, "policy_version": RECONTACT_POLICY_VERSION,
            "recontact_window_id": window["window_id"], "recontact_turn_id": window["turn_id"],
            "source_message_key": window["source_message_key"],
            "last_customer_message_key": window["last_customer_message_key"],
            "baseline_keys": json.loads(window["baseline_keys_json"]),
            "idle_seconds": window["idle_seconds"], "order_state": guard["order_state"] if guard else None,
            "identity_source": binding[0] if binding else None}


def reserve_recontact(connection: sqlite3.Connection, policy: dict[str, object], *,
                      reservation_id: str, reply_fingerprint: str, now: str) -> bool:
    if not policy["allowed"]:
        return False
    changed = connection.execute("""UPDATE cs_recontact_windows SET status='reserved',reservation_id=?,
        reply_fingerprint=?,updated_at=? WHERE window_id=? AND
        (status='eligible' OR (status='reserved' AND reservation_id=? AND reply_fingerprint=?))""",
        (reservation_id, reply_fingerprint, now, policy["recontact_window_id"], reservation_id, reply_fingerprint))
    return bool(changed.rowcount)

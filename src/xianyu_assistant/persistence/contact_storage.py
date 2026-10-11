"""Permanent first-contact/order state, independent of expiring chat records."""
from __future__ import annotations

import json
import sqlite3

from xianyu_assistant.customer_service.contact_policy import (
    CONTACT_POLICY_VERSION,
    ORDER_RANK,
    OrderEvidence,
    catalog_component,
    contains_contact_template,
    looks_like_full_catalog,
    order_evidence,
    order_state_from_text,
)
from xianyu_assistant.customer_service.fingerprints import content_fingerprint
from xianyu_assistant.customer_service.message_roles import role_is_trusted
from xianyu_assistant.customer_service.models import ConversationSnapshot
from xianyu_assistant.persistence.recontact_storage import (
    initialize_recontact_storage,
    recontact_decision,
)


def bind_contact(connection: sqlite3.Connection, conversation_key: str, now: str,
                 buyer_id: str | None = None, merchant_id: str | None = None) -> str:
    binding = connection.execute("SELECT * FROM cs_contact_bindings WHERE conversation_key=?", (conversation_key,)).fetchone()
    scoped_key = "buyer-" + content_fingerprint(merchant_id, buyer_id) if buyer_id and merchant_id and buyer_id != merchant_id else None
    key = scoped_key or (str(binding["contact_key"]) if binding else conversation_key)
    source = "merchant_buyer_id" if scoped_key else "platform_conversation"
    if binding and scoped_key and binding["identity_source"] == "merchant_buyer_id" and binding["contact_key"] != scoped_key:
        # A contradictory account/buyer binding is evidence to suppress automation,
        # never permission to merge two people or reuse an order.
        close_contact(connection, str(binding["contact_key"]), "identity_conflict", now)
        return str(binding["contact_key"])
    if binding and not scoped_key:
        source = str(binding["identity_source"])
    connection.execute("INSERT OR IGNORE INTO cs_contact_guards(contact_key,created_at,updated_at) VALUES(?,?,?)", (key, now, now))
    if binding and scoped_key and binding["contact_key"] != key:
        old = connection.execute("SELECT * FROM cs_contact_guards WHERE contact_key=?", (binding["contact_key"],)).fetchone()
        if old:
            # Transfer closed/uncertain eligibility without merging unrelated nicknames.
            connection.execute("""UPDATE cs_contact_guards SET
                closed_reason=COALESCE(closed_reason,?),
                welcome_status=CASE WHEN welcome_status='eligible' THEN ? ELSE welcome_status END,
                reservation_id=COALESCE(reservation_id,?),
                reply_fingerprint=COALESCE(reply_fingerprint,?),
                welcome_hold_reason=COALESCE(welcome_hold_reason,?),
                new_contact_evidence=COALESCE(new_contact_evidence,?) WHERE contact_key=?""",
                (old["closed_reason"], old["welcome_status"], old["reservation_id"], old["reply_fingerprint"],
                 old["welcome_hold_reason"], old["new_contact_evidence"], key))
            if old["order_state"]:
                record_order(connection, key, conversation_key, OrderEvidence(str(old["order_state"]), "identity_link", "已有订单状态", order_id=old["order_id"]), now)
    connection.execute("""INSERT INTO cs_contact_bindings(conversation_key,contact_key,identity_source,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(conversation_key) DO UPDATE SET
        contact_key=excluded.contact_key, identity_source=excluded.identity_source,updated_at=excluded.updated_at""",
        (conversation_key, key, source, now))
    return key


def close_contact(connection: sqlite3.Connection, key: str, reason: str, now: str) -> None:
    connection.execute("""UPDATE cs_contact_guards SET closed_reason=COALESCE(closed_reason,?),
        welcome_status=CASE WHEN welcome_status='eligible' THEN 'suppressed' ELSE welcome_status END,
        updated_at=? WHERE contact_key=?""", (reason, now, key))


def record_order(connection: sqlite3.Connection, key: str, conversation_key: str,
                 evidence: OrderEvidence, now: str) -> None:
    current = connection.execute("SELECT * FROM cs_contact_guards WHERE contact_key=?", (key,)).fetchone()
    if current is None:
        return
    data = evidence.as_data()
    event_key = content_fingerprint(key, conversation_key, json.dumps(data, ensure_ascii=False, sort_keys=True))
    already = connection.execute("SELECT 1 FROM cs_order_observations WHERE event_key=?", (event_key,)).fetchone()
    if already:
        return
    old_state = str(current["order_state"] or "")
    same_order = not evidence.order_id or not current["order_id"] or evidence.order_id == current["order_id"]
    # Old timeline observations cannot regress a stage. A distinct trusted header order may start a new one.
    applies = (not old_state or ORDER_RANK[evidence.state] >= ORDER_RANK.get(old_state, 0)
               or (not same_order and evidence.source == "platform_header"))
    if evidence.event_time and current["order_event_time"] and same_order and evidence.event_time < current["order_event_time"]:
        applies = False
    connection.execute("""INSERT INTO cs_order_observations(event_key,contact_key,conversation_key,
        order_state,evidence_json,applied,created_at) VALUES(?,?,?,?,?,?,?)""",
        (event_key, key, conversation_key, evidence.state, json.dumps(data, ensure_ascii=False), int(applies), now))
    if applies:
        connection.execute("""UPDATE cs_contact_guards SET order_state=?,order_id=COALESCE(?,order_id),
            order_evidence_json=?,order_event_time=?,updated_at=? WHERE contact_key=?""",
            (evidence.state, evidence.order_id, json.dumps(data, ensure_ascii=False), evidence.event_time, now, key))
    close_contact(connection, key, "order_observed", now)


def initialize_contact_storage(connection: sqlite3.Connection, now: str) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS cs_contact_guards (
            contact_key TEXT PRIMARY KEY, closed_reason TEXT,
            welcome_status TEXT NOT NULL DEFAULT 'eligible', reservation_id TEXT,
            reply_fingerprint TEXT, order_state TEXT, order_id TEXT, order_event_time TEXT,
            order_evidence_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cs_contact_bindings (
            conversation_key TEXT PRIMARY KEY, contact_key TEXT NOT NULL,
            identity_source TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_contact_binding_key ON cs_contact_bindings(contact_key);
        CREATE TABLE IF NOT EXISTS cs_order_observations (
            event_key TEXT PRIMARY KEY, contact_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
            order_state TEXT NOT NULL,evidence_json TEXT NOT NULL,applied INTEGER NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cs_contact_templates (
            template_fingerprint TEXT PRIMARY KEY, message_text TEXT NOT NULL,created_at TEXT NOT NULL);
    """)
    initialize_recontact_storage(connection)
    for column in ("role_source", "key_source", "raw_direction", "role_confidence", "role_evidence_json"):
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cs_quality_messages)")}
        if column not in columns:
            connection.execute(f"ALTER TABLE cs_quality_messages ADD COLUMN {column} TEXT")
    for column in ("welcome_hold_reason", "new_contact_evidence"):
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cs_contact_guards)")}
        if column not in columns:
            connection.execute(f"ALTER TABLE cs_contact_guards ADD COLUMN {column} TEXT")
    send_columns = {row["name"] for row in connection.execute("PRAGMA table_info(cs_quality_sends)")}
    for column, definition in (("reply_purpose", "TEXT"), ("contains_welcome", "INTEGER NOT NULL DEFAULT 0")):
        if column not in send_columns:
            connection.execute(f"ALTER TABLE cs_quality_sends ADD COLUMN {column} {definition}")
    migrated = connection.execute("SELECT 1 FROM cs_maintenance WHERE name=?", (CONTACT_POLICY_VERSION,)).fetchone()
    if migrated:
        return
    for row in connection.execute("SELECT * FROM cs_first_contact_catalog").fetchall():
        key = bind_contact(connection, str(row["conversation_key"]), now)
        connection.execute("""UPDATE cs_contact_guards SET closed_reason=COALESCE(closed_reason,'legacy_first_contact'),
            welcome_status=CASE WHEN welcome_status='eligible' THEN ? ELSE welcome_status END,
            reservation_id=COALESCE(reservation_id,?),reply_fingerprint=COALESCE(reply_fingerprint,?),updated_at=? WHERE contact_key=?""",
            (row["status"], row["reservation_id"], row["reply_fingerprint"], now, key))
    for table, where in (("cs_quality_sends", "status IN ('verified','uncertain','sending','prepared')"),
                         ("cs_quality_messages", "direction='outgoing'")):
        for row in connection.execute(f"SELECT DISTINCT conversation_key FROM {table} WHERE {where}").fetchall():
            key = bind_contact(connection, str(row["conversation_key"]), now)
            close_contact(connection, key, "legacy_merchant_activity", now)
    for row in connection.execute("SELECT conversation_key,text,platform_time,message_key FROM cs_quality_messages WHERE direction='incoming' ORDER BY observed_at,rowid").fetchall():
        state = order_state_from_text(str(row["text"] or ""))
        if state:
            key = bind_contact(connection, str(row["conversation_key"]), now)
            record_order(connection, key, str(row["conversation_key"]), OrderEvidence(state, "legacy_chat", str(row["text"] or ""), str(row["message_key"]), row["platform_time"]), now)
    for row in connection.execute("""SELECT c.order_status,c.fulfillment_status,m.conversation_key
        FROM cs_customers c JOIN cs_customer_conversations m ON m.customer_key=c.customer_key
        WHERE c.order_status IN ('pending_payment','ordered','after_sales')
        OR c.fulfillment_status IN ('pending_shipment','shipped','delivered')""").fetchall():
        state = {"pending_payment": "pending_payment", "ordered": "paid", "after_sales": "refund"}.get(str(row["order_status"]))
        if row["fulfillment_status"] in ORDER_RANK:
            state = str(row["fulfillment_status"])
        if state:
            key = bind_contact(connection, str(row["conversation_key"]), now)
            record_order(connection, key, str(row["conversation_key"]), OrderEvidence(state, "legacy_profile", "历史顾客订单档案"), now)
    for row in connection.execute("SELECT final_text,created_at FROM cs_quality_sends WHERE final_text IS NOT NULL"):
        component = catalog_component(str(row[0]))
        if component:
            connection.execute("INSERT OR IGNORE INTO cs_contact_templates(template_fingerprint,message_text,created_at) VALUES(?,?,?)",
                               (content_fingerprint(component), component, row[1]))
    templates = contact_templates(connection)
    suspect_count = 0
    cursor = connection.execute("SELECT conversation_key,text,direction,role_source FROM cs_quality_messages WHERE text IS NOT NULL")
    while rows := cursor.fetchmany(500):
        for row in rows:
            if not contains_contact_template(str(row["text"]), templates) and not looks_like_full_catalog(str(row["text"])):
                continue
            key = bind_contact(connection, str(row["conversation_key"]), now)
            if row["direction"] == "outgoing":
                close_contact(connection, key, "historical_welcome_observed", now)
            elif row["role_source"] in {None, "dom_author_structure", "dom_or_geometry", "adapter", "role_unresolved"}:
                connection.execute("UPDATE cs_contact_guards SET welcome_hold_reason=COALESCE(welcome_hold_reason,'historical_catalog_role_uncertain'),updated_at=? WHERE contact_key=?", (now, key))
                suspect_count += 1
    connection.execute("INSERT INTO cs_maintenance(name,value) VALUES(?,?)", (
        CONTACT_POLICY_VERSION, json.dumps({"migrated_at": now, "historical_catalog_holds": suspect_count})))


def contact_templates(connection: sqlite3.Connection) -> tuple[str, ...]:
    from xianyu_assistant.customer_service.current_catalog import FIRST_CONTACT_CATALOG_REPLY
    saved = connection.execute("SELECT value FROM cs_settings WHERE name='first_contact_message'").fetchone()
    return tuple(dict.fromkeys(filter(None, (
        FIRST_CONTACT_CATALOG_REPLY, str(saved[0]) if saved else None,
        *(str(r[0]) for r in connection.execute("SELECT message_text FROM cs_contact_templates")),
    ))))


def observe_contact(connection: sqlite3.Connection, snapshot: ConversationSnapshot, now: str) -> None:
    key = bind_contact(connection, snapshot.conversation_key, now, snapshot.platform_customer_id, snapshot.merchant_account_id)
    if snapshot.new_contact_evidence in {"platform_first_message", "platform_new_session"} and snapshot.platform_customer_id and snapshot.merchant_account_id:
        connection.execute("UPDATE cs_contact_guards SET new_contact_evidence=COALESCE(new_contact_evidence,?),updated_at=? WHERE contact_key=?", (snapshot.new_contact_evidence, now, key))
    templates = contact_templates(connection)
    for message in snapshot.messages:
        if contains_contact_template(message.text or "", templates) or looks_like_full_catalog(message.text or ""):
            if message.direction.value == "outgoing":
                close_contact(connection, key, "welcome_observed", now)
            elif not role_is_trusted(message.direction, message.role_source):
                connection.execute("UPDATE cs_contact_guards SET welcome_hold_reason=COALESCE(welcome_hold_reason,'catalog_role_uncertain'),updated_at=? WHERE contact_key=?", (now, key))
    if any(message.direction.value == "outgoing" for message in snapshot.messages):
        close_contact(connection, key, "merchant_history", now)
    for evidence in order_evidence(snapshot):
        record_order(connection, key, snapshot.conversation_key, evidence, now)


def contact_decision(connection: sqlite3.Connection, conversation_key: str,
                     reservation_id: str | None = None) -> dict[str, object]:
    binding = connection.execute("SELECT * FROM cs_contact_bindings WHERE conversation_key=?", (conversation_key,)).fetchone()
    key = str(binding["contact_key"]) if binding else conversation_key
    row = connection.execute("SELECT * FROM cs_contact_guards WHERE contact_key=?", (key,)).fetchone()
    reason = "new_contact"
    returning = recontact_decision(connection, conversation_key, key, row, reservation_id)
    if returning is not None:
        return returning
    if conversation_key.startswith("dom-") or not conversation_key:
        reason = "unstable_identity"
    elif row and row["order_state"]:
        reason = "order_observed"
    elif row and row["closed_reason"]:
        reason = str(row["closed_reason"])
    elif row and row["welcome_hold_reason"]:
        reason = str(row["welcome_hold_reason"])
    elif not binding or binding["identity_source"] != "merchant_buyer_id":
        reason = "contact_identity_unconfirmed"
    elif not row or not row["new_contact_evidence"]:
        reason = "new_contact_unconfirmed"
    elif row and row["welcome_status"] != "eligible":
        reason = "welcome_" + str(row["welcome_status"])
    allowed = reason == "new_contact"
    own_reservation = bool(row and row["welcome_status"] == "reserved" and reservation_id
                           and row["reservation_id"] == reservation_id and not row["order_state"]
                           and not row["closed_reason"] and not row["welcome_hold_reason"]
                           and binding and binding["identity_source"] == "merchant_buyer_id"
                           and row["new_contact_evidence"])
    if own_reservation and reason == "welcome_reserved":
        allowed, reason = True, "own_reservation"
    return {"allowed": allowed, "reason": reason, "identity_source": binding["identity_source"] if binding else "platform_conversation",
            "order_state": row["order_state"] if row else None,
            "order_evidence": json.loads(row["order_evidence_json"]) if row and row["order_evidence_json"] else None,
            "welcome_status": row["welcome_status"] if row else "eligible",
            "welcome_hold_reason": row["welcome_hold_reason"] if row else None,
            "new_contact_evidence": row["new_contact_evidence"] if row else None,
            "policy_version": CONTACT_POLICY_VERSION}

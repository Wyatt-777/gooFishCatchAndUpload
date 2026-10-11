"""Offline rule and migration checks. Never connects a browser/LLM or sends messages."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from xianyu_assistant.customer_service.battery_models import model_selections
from xianyu_assistant.customer_service.contact_policy import guarded_reply, order_state_from_text
from xianyu_assistant.customer_service.current_catalog import FIRST_CONTACT_CATALOG_REPLY
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def table_digest(connection: sqlite3.Connection, table: str, columns: list[str]) -> str:
    digest = hashlib.sha256()
    names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
    for row in connection.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    copied = args.output / 'migration-copy.db'
    with sqlite3.connect(args.source.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(copied) as target:
        source.backup(target)
    before = {}
    with sqlite3.connect(copied) as connection:
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
            before[name] = (columns, table_digest(connection, name, columns))
    repository = CustomerServiceRepository(copied)
    repository.initialize()
    changed, unchanged = [], []
    with sqlite3.connect(copied) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for name, (columns, digest) in before.items():
            (unchanged if table_digest(connection, name, columns) == digest else changed).append(name)
        assert set(changed) <= {'cs_maintenance', 'cs_first_contact_catalog', 'cs_customers', 'cs_customer_conversations'}
        first = connection.execute('SELECT COUNT(*) FROM cs_contact_guards').fetchone()[0]
    repository.initialize()
    with sqlite3.connect(copied) as connection:
        assert connection.execute('SELECT COUNT(*) FROM cs_contact_guards').fetchone()[0] == first

    cases = {
        '我已拍下，待付款': 'pending_payment',
        '我拍了': 'pending_payment', '已下单': 'pending_payment',
        '我已拍下，待付款\n请双方沟通及时确认价格\n修改价格': 'pending_payment',
        '我已付款，等待你发货': 'paid', '已支付': 'paid',
        '我拍下了，付款了': 'paid', '没付款，但我已拍下': 'pending_payment',
        '未付款': None, '还没拍下': None, '我没有付款': None,
        '如果我拍下了呢': None, '我已付款吗': None,
        '我发起了退款申请': 'refund', '订单已取消': 'cancelled',
    }
    for text, state in cases.items():
        assert order_state_from_text(text) == state, text
    for text in ('60～20', '60~20', '60-20', '60 20', '6020', '60V/20A'):
        assert model_selections(text) == (('6020', '60V20Ah'),), text
    assert model_selections('48V/30A') == (('4830', '48V30Ah'),)
    assert model_selections('6030元') == ()
    catalog = FIRST_CONTACT_CATALOG_REPLY
    cleaned, blocked = guarded_reply(catalog, allowed=False, templates=(catalog,), query='我已拍下，待付款', known_order=True, requested_catalog=False)
    assert blocked and '我核对' in cleaned and '60V30' not in cleaned
    cleaned, blocked = guarded_reply('收到\n\n' + catalog + '\n广东普宁自提', allowed=False, templates=(catalog,), query='自提在哪', known_order=True, requested_catalog=False)
    assert blocked and '广东普宁自提' in cleaned and '60V30' not in cleaned
    assert guarded_reply(catalog, allowed=False, templates=(catalog,), query='再发价格表', known_order=True, requested_catalog=True) == (catalog, False)

    # Separate temporary database checks transactional state, with no chat engine.
    state_db = args.output / f'state-checks-{uuid4().hex[:8]}.db'
    state_repo = CustomerServiceRepository(state_db)
    state_repo.initialize()
    now = datetime.now().astimezone()
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-new', ()), observed_at=now)
    def reserve(number: int) -> bool:
        return state_repo.reserve_first_contact_catalog('session-new', reservation_id=f'reservation-{number}', reply_fingerprint='fp', created_at=now)
    with ThreadPoolExecutor(max_workers=4) as executor:
        reservations = list(executor.map(reserve, range(4)))
    assert sum(reservations) == 1
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-order', (), order_state_text='等待买家付款'), observed_at=now)
    assert state_repo.contact_policy('session-order')['order_state'] == 'pending_payment'
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-order', ()), observed_at=now + timedelta(seconds=1))
    assert not state_repo.contact_policy('session-order')['allowed']
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-order', (), order_state_text='等待卖家发货'), observed_at=now + timedelta(seconds=2))
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-order', (), order_state_text='等待买家付款'), observed_at=now + timedelta(seconds=3))
    assert state_repo.contact_policy('session-order')['order_state'] == 'paid'
    for key in ('session-alias1', 'session-alias2'):
        state_repo.observe_contact_snapshot(ConversationSnapshot(key, (), platform_customer_id='buyer', merchant_account_id='shop'), observed_at=now)
    assert state_repo.reserve_first_contact_catalog('session-alias1', reservation_id='alias-reservation', reply_fingerprint='fp', created_at=now)
    assert not state_repo.contact_policy('session-alias2')['allowed']
    # Final pre-click order evidence closes eligibility even after reservation.
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-late-order', ()), observed_at=now)
    assert state_repo.reserve_first_contact_catalog('session-late-order', reservation_id='late-send', reply_fingerprint='fp', created_at=now)
    state_repo.record_quality_send(send_id='late-send', run_id=None, conversation_key='session-late-order',
        job_id=None, mode='auto_send', origin='application_auto', draft_text=catalog, final_text=catalog, created_at=now)
    state_repo.record_send_contact_policy('late-send', purpose='welcome', contains_welcome=True)
    assert state_repo.check_contact_submission('late-send', {}, observed_at=now) is None
    assert state_repo.check_contact_submission('late-send', {'order_observations': [{
        'state': 'pending_payment', 'source': 'platform_header', 'text': '等待买家付款',
        'message_key': '', 'event_time': None, 'order_id': 'order-new',
    }]}, observed_at=now) is not None
    # Verified platform ID proof corrects an echo; buyer text copy with another ID is retained.
    state_repo.update_quality_send('late-send', status='verified', updated_at=now, outgoing_message_key='platform-message')
    echo = ChatMessage('platform-message', MessageDirection.INCOMING, MessageKind.TEXT,
                       text=catalog, key_source='platform_attribute')
    buyer_copy = ChatMessage('different-platform-message', MessageDirection.INCOMING, MessageKind.TEXT,
                             text=catalog, key_source='platform_attribute')
    normalized = state_repo.normalize_snapshot_roles(ConversationSnapshot('session-late-order', (echo, buyer_copy)))
    assert normalized.messages[0].direction is MessageDirection.OUTGOING
    assert normalized.messages[1].direction is MessageDirection.INCOMING
    state_repo.observe_contact_snapshot(ConversationSnapshot('session-other-shop', (), platform_customer_id='buyer', merchant_account_id='other-shop'), observed_at=now)
    assert state_repo.contact_policy('session-other-shop')['allowed']
    state_repo.initialize()
    assert not state_repo.contact_policy('session-order')['allowed']
    state_repo.cleanup_expired_audit(now=now + timedelta(days=100))
    assert not state_repo.contact_policy('session-order')['allowed']
    assert not state_repo.contact_policy('session-alias2')['allowed']
    bundle = args.output / 'export-check.zip'
    export_quality_bundle(state_db, bundle, days=30, include_raw_text=False)
    import zipfile
    with zipfile.ZipFile(bundle) as archive:
        assert {'contact_guards.jsonl', 'contact_bindings.jsonl', 'order_observations.jsonl'} <= set(archive.namelist())
    result = {'integrity': 'ok', 'migration_idempotent': True,
              'existing_tables_unchanged': unchanged, 'existing_tables_changed': changed,
              'migrated_contact_count': first, 'order_rule_checks': len(cases),
              'model_checks': 8, 'parallel_reservation_successes': sum(reservations),
              'order_state_survives_restart_and_cleanup': True,
              'same_buyer_alias_and_shop_isolation': True, 'quality_export': 'ok',
              'final_submission_order_gate': True, 'verified_role_proof_without_text_blacklist': True,
              'browser_calls': 0, 'llm_calls': 0, 'message_submissions': 0}
    (args.output / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

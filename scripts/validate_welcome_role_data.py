"""Offline uploaded-data, schema and transaction checks; no browser/LLM/chat simulation."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from xianyu_assistant.browser.executor_protocol import pack_message, unpack_message
from xianyu_assistant.customer_service.contact_policy import (
    CONTACT_POLICY_VERSION,
    contains_contact_template,
    looks_like_full_catalog,
    quoted_customer_question,
)
from xianyu_assistant.customer_service.message_roles import resolve_role
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def digest_table(db: sqlite3.Connection, table: str, columns: list[str]) -> str:
    columns_sql = ','.join('"' + c.replace('"', '""') + '"' for c in columns)
    digest = hashlib.sha256()
    for row in db.execute(f'SELECT {columns_sql} FROM "{table}" ORDER BY rowid'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, default=str).encode())
    return digest.hexdigest()


def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    copied = args.output / 'migration-copy.db'
    if copied.exists():
        raise RuntimeError('Use a new output directory to preserve earlier validation evidence')
    with sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(copied) as target:
        source.backup(target)
    before = {}
    with sqlite3.connect(copied) as db:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            before[table] = (columns, digest_table(db, table, columns))
    migrated = CustomerServiceRepository(copied)
    started = datetime.now().astimezone()
    migrated.initialize(migrate_confirmed_facts=False)
    migration_seconds = (datetime.now().astimezone()-started).total_seconds()
    changed, unchanged = [], []
    with sqlite3.connect(copied) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for table, (columns, old) in before.items():
            (unchanged if old == digest_table(db, table, columns) else changed).append(table)
        assert set(changed) <= {'cs_contact_guards','cs_contact_bindings','cs_maintenance','cs_first_contact_catalog','cs_customers','cs_customer_conversations','cs_contact_templates'}, changed
        guard_before = db.execute('SELECT * FROM cs_contact_guards ORDER BY contact_key').fetchall()
        migration = json.loads(db.execute('SELECT value FROM cs_maintenance WHERE name=?',(CONTACT_POLICY_VERSION,)).fetchone()[0])
    migrated.initialize(migrate_confirmed_facts=False)
    with sqlite3.connect(copied) as db:
        assert guard_before == db.execute('SELECT * FROM cs_contact_guards ORDER BY contact_key').fetchall()

    with zipfile.ZipFile(args.bundle) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in manifest['sha256'].items())
        messages = [json.loads(x) for x in z.read('messages.jsonl').decode('utf-8').splitlines()]
        decisions = [json.loads(x) for x in z.read('decisions.jsonl').decode('utf-8').splitlines()]
        sends = [json.loads(x) for x in z.read('sends.jsonl').decode('utf-8').splitlines()]
    bykey={(r['conversation_key'],r['message_key']):r for r in messages}
    decindex={r['turn_id']:r for r in decisions}
    repo=CustomerServiceRepository(args.output/'uploaded-evidence-checks.db')
    repo.initialize(migrate_confirmed_facts=False)
    templates=repo.contact_templates()
    proofs=[]
    for send in sends:
        if send['created_at']<'2026-10-06T18:41' or not send['contains_welcome']:
            continue
        dec=decindex.get(send['turn_id'])
        if not dec: continue
        rows=[bykey.get((send['conversation_key'],k)) for k in dec['customer_message_keys_json']]
        rows=[r for r in rows if r and r['observed_at']<=send['created_at'] and
              (contains_contact_template(r['text'] or '', templates) or looks_like_full_catalog(r['text'] or ''))]
        if not rows: continue
        raw=ConversationSnapshot(send['conversation_key'], tuple(ChatMessage(
            r['message_key'],MessageDirection(r['direction']),MessageKind(r['message_type']),
            text=r['text'],role_source=r['role_source'] or 'legacy_unknown',key_source=r['key_source'] or 'legacy_unknown',
        ) for r in rows))
        normalized=repo.normalize_snapshot_roles(raw)
        assert all(m.direction is MessageDirection.UNKNOWN for m in normalized.messages)
        repo.observe_contact_snapshot(normalized,observed_at=datetime.fromisoformat(send['created_at']))
        decision=repo.contact_policy(raw.conversation_key)
        assert not decision['allowed'] and decision['reason']=='catalog_role_uncertain', decision
        repo.record_quality_snapshot(normalized,run_id=None,observed_at=datetime.fromisoformat(send['created_at']))
        proofs.append({'send_id':send['send_id'],'conversation_key':raw.conversation_key,'blocked_reason':decision['reason'],
                       'input_direction':'incoming','normalized_direction':'unknown','source_status':send['status']})
    assert len(proofs)==27, len(proofs)
    assert unpack_message(pack_message({'snapshot':normalized}))['snapshot'] == normalized

    # State/role contracts use isolated tables and scalar evidence only; they do
    # not run a chat worker, create model output, or submit browser actions.
    now=datetime.now().astimezone()
    key=proofs[0]['conversation_key']+'-transaction-check'
    buyer=proofs[0]['conversation_key']
    for alias in (key,key+'-alias'):
        repo.observe_contact_snapshot(ConversationSnapshot(alias,(),platform_customer_id=buyer,
            merchant_account_id='validation-account',new_contact_evidence='platform_first_message'),observed_at=now)
    def reserve(i: int) -> bool:
        return repo.reserve_first_contact_catalog(key,reservation_id=f'db-reservation-{i}',reply_fingerprint='contract-only',created_at=now)
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations=list(pool.map(reserve,range(4)))
    assert sum(reservations)==1
    assert not repo.contact_policy(key+'-alias')['allowed']
    winner = f'db-reservation-{reservations.index(True)}'
    repo.record_quality_send(send_id=winner, run_id=None, conversation_key=key, job_id=None,
                             mode='auto_send', origin='application_auto', draft_text=None,
                             final_text=templates[0], created_at=now)
    repo.record_send_contact_policy(winner, purpose='welcome', contains_welcome=True)
    evidence={'merchant_account_id':'validation-account','platform_customer_id':buyer}
    assert repo.check_contact_submission(winner,evidence,observed_at=now) is None
    assert repo.check_contact_submission(winner,{},observed_at=now) is not None
    assert repo.check_contact_submission(winner,{**evidence,'platform_customer_id':buyer+'-other'},observed_at=now) is not None
    missing=key+'-missing'
    repo.observe_contact_snapshot(ConversationSnapshot(missing,()),observed_at=now)
    assert repo.contact_policy(missing)['reason']=='contact_identity_unconfirmed'
    no_new=key+'-old-contact'
    repo.observe_contact_snapshot(ConversationSnapshot(no_new,(),platform_customer_id=no_new,
        merchant_account_id='validation-account'),observed_at=now)
    assert repo.contact_policy(no_new)['reason']=='new_contact_unconfirmed'
    repo.observe_contact_snapshot(ConversationSnapshot(key+'-order',(),order_state_text='等待买家付款'),observed_at=now)
    assert not repo.contact_policy(key+'-order')['allowed']
    repo.set_setting('first_contact_message',templates[0])
    repo.initialize(migrate_confirmed_facts=False)
    repo.cleanup_expired_audit(now=now+timedelta(days=100))
    assert not repo.contact_policy(key+'-alias')['allowed']
    assert not repo.contact_policy(key+'-order')['allowed']
    for proof in proofs:
        assert not repo.contact_policy(proof['conversation_key'])['allowed']

    direction,source,_=resolve_role({'structure_incoming':True,'geometry':'outgoing'})
    assert (direction,source)==(MessageDirection.OUTGOING,'geometry')
    assert resolve_role({'structure_incoming':True})[0] is MessageDirection.UNKNOWN
    assert resolve_role({'raw_direction':'incoming','geometry':'outgoing'})[0] is MessageDirection.UNKNOWN
    assert resolve_role({'sender_id':buyer},merchant_id='validation-account',buyer_id=buyer)[0] is MessageDirection.INCOMING
    assert resolve_role({'sender_id':'validation-account'},merchant_id='validation-account',buyer_id=buyer)[0] is MessageDirection.OUTGOING
    assert quoted_customer_question(templates[0]+'\n6030的实际容量是多少？',templates)=='6030的实际容量是多少？'
    incoming=replace(normalized.messages[0],direction=MessageDirection.INCOMING,
                     role_source='platform_self_flag',role_confidence='trusted')
    assert repo.normalize_snapshot_roles(replace(normalized,messages=(incoming,))).messages[0].direction is MessageDirection.INCOMING
    bundle=args.output/'export-check.zip'
    export_quality_bundle(repo.database_path if hasattr(repo,'database_path') else args.output/'uploaded-evidence-checks.db',bundle,days=30,include_raw_text=False)
    with zipfile.ZipFile(bundle) as z:
        assert json.loads(z.read('manifest.json'))['schema_version']==4
    report={'success':True,'migration_seconds':migration_seconds,'migration_idempotent':True,'migration':migration,
            'unchanged_existing_tables':unchanged,'changed_existing_tables':changed,'uploaded_cases':proofs,
            'blocked_uploaded_case_count':len(proofs),'parallel_reservation_winners':sum(reservations),
            'submission_identity_and_missing_identity_blocked':True,
            'missing_identity_and_new_evidence_blocked':True,'alias_restart_cleanup_order_and_template_edit':True,
            'buyer_quote_keeps_customer_role':True,'role_resolution_contracts':5,'quote_question_preserved':True,
            'quality_schema':4,'browser_calls':0,'llm_calls':0,'message_submissions':0,
            'role_snapshot_ipc_roundtrip':True,
            'real_dom_role_accuracy':'requires_real_environment_observation'}
    (args.output/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'success':True,'blocked_uploaded_cases':len(proofs),'migration_seconds':migration_seconds,
                      'report':str(args.output/'validation.json'),'browser_calls':0,'llm_calls':0},ensure_ascii=False))


if __name__=='__main__':
    main()

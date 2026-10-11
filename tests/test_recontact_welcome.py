"""Real SQLite/worker regressions for inactivity-based welcome windows."""
from __future__ import annotations

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_reply_questions import Clock, OfflinePage, message, poll_reply, snapshot, worker

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    FIRST_CONTACT_MESSAGE_SETTING,
)
from xianyu_assistant.customer_service.fingerprints import batch_fingerprint
from xianyu_assistant.customer_service.models import MessageDirection, ReceptionMode, ReplyJobStatus
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.persistence.recontact_storage import is_recontact_question

WELCOME = "你好，欢迎咨询，有什么想了解的？"


@pytest.fixture
def repo(tmp_path: Path) -> CustomerServiceRepository:
    value = CustomerServiceRepository(tmp_path / "recontact.db")
    value.initialize(migrate_confirmed_facts=False)
    for product in CURRENT_PRODUCTS:
        value.save_product_knowledge(product)
    value.set_setting(FIRST_CONTACT_MESSAGE_SETTING, WELCOME)
    return value


def observe(repo: CustomerServiceRepository, clock: Clock, key: str, text: str, **bindings):
    current = message(key, text)
    snap = replace(snapshot(current), **bindings)
    repo.observe_contact_snapshot(snap, observed_at=clock.now())
    turn_id = batch_fingerprint(snap.conversation_key, ((key, text),))
    repo.observe_customer_contact_turn(snap, (current,), turn_id=turn_id, observed_at=clock.now())
    return snap, turn_id


@pytest.mark.parametrize("seconds,welcome", [(899, False), (900, True), (901, True)])
def test_full_worker_boundary_old_history_and_one_submission(
    repo: CustomerServiceRepository, seconds: int, welcome: bool,
) -> None:
    clock = Clock()
    page = OfflinePage(replace(snapshot(message("first", "60V30多少钱")),
                               new_contact_evidence="platform_first_message"))
    service = worker(page, repo, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert len(page.sent) == 1 and WELCOME in page.sent[0]
        clock.advance(seconds - 7)
        page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                (message("second", "60V30尺寸多大"),), new_contact_evidence=None)
        poll_reply(service, clock)
        assert len(page.sent) == 2 and "17×18×32" in page.sent[-1]
        assert (WELCOME in page.sent[-1]) is welcome
        for _ in range(3):
            poll_reply(service, clock)
        assert len(page.sent) == 2
        with repo._connection() as db:
            assert db.execute("SELECT count(*) FROM cs_submission_tombstones WHERE permission_issued=1").fetchone()[0] == 2
            rows = db.execute("SELECT contains_welcome FROM cs_quality_sends ORDER BY created_at").fetchall()
            assert [r[0] for r in rows] == [1, int(welcome)]
            if welcome:
                assert db.execute("SELECT status FROM cs_recontact_windows").fetchone()[0] == "sent"
    finally:
        service.close()


def test_followups_refresh_the_inactivity_clock(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(600)
    observe(repo, clock, "followup", "包邮吗")
    clock.advance(600)
    observe(repo, clock, "new", "60V30尺寸多大")
    assert not repo.contact_policy("chat")["allowed"]
    clock.advance(900)
    observe(repo, clock, "later", "60V30能跑多远")
    assert repo.contact_policy("chat")["allowed"]
    assert repo.contact_policy("chat")["idle_seconds"] == 900


@pytest.mark.parametrize("text", ["收到", "谢谢", "好的", "60V30", "我已拍下", "60V30多少钱？"])
def test_gap_alone_does_not_trigger_ack_selection_order_or_same_question(
    repo: CustomerServiceRepository, text: str,
) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(901)
    observe(repo, clock, "later", text)
    assert not repo.contact_policy("chat")["allowed"]


def test_restart_and_rereading_do_not_refresh_time_or_repeat_welcome(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    page = OfflinePage(snapshot(message("old-merchant", "之前已接待", MessageDirection.OUTGOING),
                                message("first", "60V30多少钱")))
    service = worker(page, repo, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert WELCOME not in page.sent[-1]
        service.stop()
        service.close()
        clock.advance(901 - 7)
        repo = CustomerServiceRepository(repo.database_path)
        service = worker(page, repo, clock)
        service.start()
        for _ in range(3):
            service.run_once()
        assert len(page.sent) == 1
        page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                (message("new", "60V30能跑多远"),))
        poll_reply(service, clock)
        assert len(page.sent) == 2 and WELCOME in page.sent[-1] and "50-60公里" in page.sent[-1]
        service.stop()
        service.close()
        service = worker(page, CustomerServiceRepository(repo.database_path), clock)
        service.start()
        poll_reply(service, clock)
        assert len(page.sent) == 2
    finally:
        service.close()


@pytest.mark.parametrize("block", ["order", "human", "pending", "identity", "role_hold"])
def test_gap_never_reopens_hard_safety_locks(repo: CustomerServiceRepository, block: str) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(901)
    observe(repo, clock, "later", "60V30能跑多远")
    assert repo.contact_policy("chat")["allowed"]
    with repo._connection() as db:
        if block == "order":
            db.execute("UPDATE cs_contact_guards SET order_state='pending_payment'")
        elif block == "identity":
            db.execute("UPDATE cs_contact_guards SET closed_reason='identity_conflict'")
        elif block == "role_hold":
            db.execute("UPDATE cs_contact_guards SET welcome_hold_reason='role_uncertain'")
    if block == "human":
        repo.observe_customer_turn(turn_id="human", conversation_key="chat", message_keys=("later",),
                                   customer_text="人工处理", platform_product_id="listing", observed_at=clock.now())
        with repo._connection() as db:
            db.execute("UPDATE cs_conversation_states SET human_owned=1")
    elif block == "pending":
        repo.record_quality_send(send_id="pending", run_id=None, conversation_key="chat", job_id="pending",
                                 mode="auto_send", origin="application_auto", draft_text="之前回复",
                                 final_text="之前回复", created_at=clock.now())
        repo.update_quality_send("pending", status="uncertain", updated_at=clock.now())
    assert not repo.contact_policy("chat")["allowed"]


def test_atomic_window_reservation_allows_only_one_job(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    observe(repo, clock, "new", "60V30尺寸多大")
    def reserve(job):
        return repo.reserve_first_contact_catalog("chat", reservation_id=job, reply_fingerprint="welcome",
                                                  created_at=clock.now())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("job1", "job2")))
    assert sum(results) == 1
    owner = "job1" if results[0] else "job2"
    assert reserve(owner)
    repo.release_first_contact_catalog_reservation("chat", reservation_id=owner)
    assert reserve("job3")
    repo.mark_first_contact_catalog_sent("chat", reservation_id="job3", updated_at=clock.now())
    assert not reserve("job4")


def test_same_buyer_across_products_shares_clock_and_other_buyers_are_isolated(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(899)
    observe(repo, clock, "second", "60V30尺寸多大", conversation_key="another", platform_product_id="other")
    assert not repo.contact_policy("another")["allowed"]
    clock.advance(900)
    observe(repo, clock, "third", "60V30能跑多远", conversation_key="third")
    assert repo.contact_policy("third")["allowed"]
    observe(repo, clock, "other-buyer", "60V30能跑多远", platform_customer_id="buyer2",
            conversation_key="other-buyer-chat")
    assert not repo.contact_policy("other-buyer-chat")["allowed"]
    assert repo.contact_policy("third")["allowed"]


def test_old_timestamp_and_delayed_draft_do_not_become_eligible(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    first, _ = observe(repo, clock, "first", "60V30多少钱")
    clock.advance(899)
    current, turn = observe(repo, clock, "current", "60V30尺寸多大")
    assert not repo.contact_policy("chat")["allowed"]
    clock.advance(900)
    repo.observe_customer_contact_turn(current, current.messages, turn_id=turn, observed_at=clock.now())
    assert not repo.contact_policy("chat")["allowed"]
    old = replace(message("old-unseen", "60V30能跑多远"), platform_time=clock.now()-timedelta(hours=2))
    old_snapshot = replace(first, messages=(old,))
    repo.observe_customer_contact_turn(old_snapshot, (old,), turn_id="old", observed_at=clock.now())
    assert not repo.contact_policy("chat")["allowed"]


def test_burst_keeps_qualified_window_but_manual_intervention_cancels_it(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(901)
    snap, _ = observe(repo, clock, "new", "60V30尺寸多大")
    detail = message("detail", "60V30Ah")
    snap = replace(snap, messages=snap.messages + (detail,))
    turn = batch_fingerprint("chat", ((m.message_key, m.text) for m in snap.messages))
    repo.observe_customer_contact_turn(snap, snap.messages, turn_id=turn, observed_at=clock.now())
    assert repo.contact_policy("chat")["recontact_turn_id"] == turn
    service = worker(OfflinePage(snap), repo, clock)
    try:
        assert service._contact_policy_for(snap)["allowed"]
        manual = replace(snap, messages=(message("manual", "我来处理", MessageDirection.OUTGOING), *snap.messages))
        assert not service._contact_policy_for(manual)["allowed"]
    finally:
        service.close()


def test_export_anonymizes_window_links_and_keeps_gap(repo: CustomerServiceRepository, tmp_path: Path) -> None:
    clock = Clock()
    observe(repo, clock, "private-first", "60V30多少钱")
    clock.advance(901)
    observe(repo, clock, "private-new", "60V30尺寸多大")
    target = tmp_path / "quality.zip"
    export_quality_bundle(repo.database_path, target, now=clock.now())
    with zipfile.ZipFile(target) as archive:
        row = json.loads(archive.read("recontact_windows.jsonl"))
        seen = archive.read("contact_seen_messages.jsonl").decode()
        assert row["idle_seconds"] == 901
        assert "private-new" not in json.dumps(row) and "private-first" not in seen
        assert row["source_message_key"].startswith("msg-")


def test_worker_burst_carries_window_to_new_batch(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30尺寸多大")
    clock.advance(901)
    page = OfflinePage(snapshot(message("new", "多少钱")))
    service = worker(page, repo, clock)
    try:
        service.start()
        service.run_once()
        assert repo.contact_policy("chat")["allowed"]
        page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                (message("detail", "60V30多少钱"),))
        poll_reply(service, clock)
        assert len(page.sent) == 1 and WELCOME in page.sent[0] and "678" in page.sent[0]
        with repo._connection() as db:
            row = db.execute("SELECT status,last_customer_message_key FROM cs_recontact_windows").fetchone()
            assert tuple(row) == ("sent", "detail")
    finally:
        service.close()


def test_old_reserved_window_survives_restart_and_new_question(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    observe(repo, clock, "second", "60V30尺寸多大")
    assert repo.reserve_first_contact_catalog("chat", reservation_id="crashed-job",
                                              reply_fingerprint="welcome", created_at=clock.now())
    restarted = CustomerServiceRepository(repo.database_path)
    clock.advance(900)
    observe(restarted, clock, "third", "60V30能跑多远")
    assert restarted.contact_policy("chat")["reason"] == "recontact_reserved"
    assert not restarted.reserve_first_contact_catalog("chat", reservation_id="new-job",
                                                       reply_fingerprint="welcome", created_at=clock.now())


@pytest.mark.parametrize("change", ["buyer_missing", "merchant_missing", "role", "message_id", "unknown"])
def test_untrusted_arrivals_cannot_open_a_window(repo: CustomerServiceRepository, change: str) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    current = message("new", "60V30尺寸多大")
    snap = snapshot(current)
    if change == "buyer_missing":
        snap = replace(snap, platform_customer_id=None)
    elif change == "merchant_missing":
        snap = replace(snap, merchant_account_id=None)
    elif change == "role":
        current = replace(current, role_source="unknown")
        snap = replace(snap, messages=(current,))
    elif change == "message_id":
        current = replace(current, key_source="content_fallback")
        snap = replace(snap, messages=(current,))
    else:
        snap = replace(snap, messages=(message("unknown", "身份不明", MessageDirection.UNKNOWN), current))
    repo.observe_customer_contact_turn(snap, (current,), turn_id="new-turn", observed_at=clock.now())
    with repo._connection() as db:
        assert db.execute("SELECT count(*) FROM cs_recontact_windows").fetchone()[0] == 0


@pytest.mark.parametrize("seconds", [899, 900])
def test_human_review_keeps_arrival_time_even_after_delayed_approval(
    repo: CustomerServiceRepository, seconds: int,
) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(seconds)
    page = OfflinePage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        assert draft.status is ReplyJobStatus.AWAITING_REVIEW and not page.sent
        assert (WELCOME in draft.reply_text) is (seconds == 900)
        clock.advance(900)
        result = service.approve_draft(draft.job_id)
        assert result.sent and len(page.sent) == 1
        assert (WELCOME in page.sent[0]) is (seconds == 900)
    finally:
        service.close()


@pytest.mark.parametrize("change", ["manual", "customer", "identity", "order"])
def test_review_reread_rejects_changed_context(repo: CustomerServiceRepository, change: str) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    page = OfflinePage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        assert WELCOME in draft.reply_text
        if change == "manual":
            page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                    (message("manual", "我来处理", MessageDirection.OUTGOING),))
        elif change == "customer":
            page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                    (message("newer", "60V30能跑多远"),))
        elif change == "identity":
            page.snapshot = replace(page.snapshot, platform_customer_id="another-buyer")
        else:
            page.snapshot = replace(page.snapshot, messages=page.snapshot.messages +
                                    (message("order", "我已拍下"),))
        result = service.approve_draft(draft.job_id)
        assert not result.sent and not page.sent
    finally:
        service.close()


def test_default_catalog_still_answers_the_new_question(repo: CustomerServiceRepository) -> None:
    from xianyu_assistant.customer_service.current_catalog import configured_catalog_reply
    repo.delete_setting(FIRST_CONTACT_MESSAGE_SETTING)
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    page = OfflinePage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert len(page.sent) == 1 and page.sent[0].startswith(configured_catalog_reply(repo))
        assert "17×18×32" in page.sent[0]
    finally:
        service.close()


def test_old_unanswered_question_does_not_make_new_model_selection_a_question(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    first = message("first", "尺寸多大")
    snap = snapshot(first)
    repo.observe_customer_contact_turn(snap, (first,), turn_id="first", observed_at=clock.now())
    clock.advance(900)
    selection = message("selection", "60V30")
    snap = replace(snap, messages=(first, selection))
    repo.observe_customer_contact_turn(snap, snap.messages, turn_id="second", observed_at=clock.now())
    with repo._connection() as db:
        assert db.execute("SELECT count(*) FROM cs_recontact_windows").fetchone()[0] == 0


def test_default_catalog_does_not_allow_wrong_dimension_in_review(repo: CustomerServiceRepository) -> None:
    repo.delete_setting(FIRST_CONTACT_MESSAGE_SETTING)
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    page = OfflinePage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        assert draft.status is ReplyJobStatus.AWAITING_REVIEW
        result = service.approve_draft(draft.job_id, draft.reply_text.replace("17×18×32", "14×18×32"))
        assert not result.sent and not page.sent and "尺寸" in result.reason
    finally:
        service.close()


def test_restart_restores_qualified_review_draft_once(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    page = OfflinePage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        assert WELCOME in draft.reply_text and not page.sent
        service.stop()
        service.close()
        service = worker(page, CustomerServiceRepository(repo.database_path), clock, ReceptionMode.HUMAN_CONFIRMATION)
        service.start()
        poll_reply(service, clock)
        restored = service.drafts.list()[-1]
        assert restored.job_id == draft.job_id and WELCOME in restored.reply_text
        assert service.approve_draft(restored.job_id).sent
        assert not service.approve_draft(restored.job_id).sent and len(page.sent) == 1
    finally:
        service.close()


def test_additive_upgrade_preserves_old_welcome_and_order_locks(repo: CustomerServiceRepository) -> None:
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    with repo._connection() as db:
        db.execute("UPDATE cs_contact_guards SET welcome_status='sent',closed_reason='welcome_sent',order_state='paid'")
        before = tuple(db.execute("SELECT * FROM cs_contact_guards").fetchone())
        for table in ("cs_contact_activity", "cs_contact_seen_messages", "cs_recontact_windows"):
            db.execute("DROP TABLE " + table)
    restarted = CustomerServiceRepository(repo.database_path)
    restarted.initialize(migrate_confirmed_facts=False)
    with restarted._connection() as db:
        assert tuple(db.execute("SELECT * FROM cs_contact_guards").fetchone()) == before
    clock.advance(900)
    observe(restarted, clock, "second", "60V30尺寸多大")
    clock.advance(900)
    observe(restarted, clock, "third", "60V30能跑多远")
    assert not restarted.contact_policy("chat")["allowed"]


@pytest.mark.parametrize("change", ["manual", "customer", "unknown_role"])
def test_final_click_checkpoint_cancels_intervening_evidence(repo: CustomerServiceRepository, change: str) -> None:
    class IntervenedPage(OfflinePage):
        def set_send_checkpoint(self, callback):
            def final_check(evidence):
                if change == "customer":
                    evidence["last_customer_key"] = "newer-customer-message"
                elif change == "unknown_role":
                    evidence["role_uncertain"] = True
                else:
                    evidence["contact_message_evidence"].append({
                        "message_key": "manual-between-read-and-click", "direction": "outgoing",
                        "role_source": "geometry", "text": "我来处理"})
                return callback(evidence)
            self.checkpoint = final_check if callback else None
    clock = Clock()
    observe(repo, clock, "first", "60V30多少钱")
    clock.advance(900)
    page = IntervenedPage(snapshot(message("new", "60V30尺寸多大")))
    service = worker(page, repo, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert not page.sent
        with repo._connection() as db:
            assert db.execute("SELECT count(*) FROM cs_submission_tombstones WHERE permission_issued=1").fetchone()[0] == 0
    finally:
        service.close()


@pytest.mark.parametrize("question", ["尺寸多大", "有没有货", "能跑多远", "支持蓝牙吗", "支持蓝牙吗。", "500行吗"])
def test_question_detection_preserves_real_questions(question: str) -> None:
    assert is_recontact_question(question)

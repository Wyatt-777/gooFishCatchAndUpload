"""Real regression for omitted Ah and evidence-bound attribute continuation."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xianyu_assistant.customer_service.battery_models import model_selections
from xianyu_assistant.customer_service.current_catalog import CURRENT_PRODUCTS
from xianyu_assistant.customer_service.customer_service_worker import (
    CustomerServiceWorker,
    _trusted_quantity_totals,
)
from xianyu_assistant.customer_service.fact_guard import (
    FactGuardError,
    ProductMatch,
    critical_answer,
    resolve_products,
    validate_reply,
)
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    CustomerServiceConfig,
    PageHealth,
    PageHealthStatus,
    ReceptionMode,
    ReceptionStatus,
    ReplyJobStatus,
    SendReceipt,
)
from xianyu_assistant.customer_service.models import (
    MessageDirection as Direction,
)
from xianyu_assistant.customer_service.models import (
    MessageKind as Kind,
)
from xianyu_assistant.customer_service.negotiation import (
    build_negotiation_plan,
    quantity_from_messages,
)
from xianyu_assistant.customer_service.quality_export import _diagnostic_refs
from xianyu_assistant.customer_service.reply_questions import (
    ReplyQuestion,
    resolve_reply_question,
    validate_question_reply,
)
from xianyu_assistant.customer_service.semantic_analysis import (
    analyze_customer_turn,
    parse_and_guard_model_semantics,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def message(key: str, text: str, direction: Direction = Direction.INCOMING) -> ChatMessage:
    return ChatMessage(key, direction, Kind.TEXT, text, role_source="geometry",
                       key_source="react_message_id")


def snapshot(*messages: ChatMessage) -> ConversationSnapshot:
    return ConversationSnapshot("chat", tuple(messages), platform_product_id="listing",
                                merchant_account_id="seller", platform_customer_id="buyer")


@pytest.mark.parametrize("query,canonical", [
    ("60V30", "60V30Ah"), ("60v 20", "60V20Ah"), ("48V30", "48V30Ah"),
    ("60伏30", "60V30Ah"), ("６０Ｖ３０", "60V30Ah"), ("60V30Ah", "60V30Ah"),
    ("6030", "60V30Ah"), ("60-30", "60V30Ah"), ("60 30", "60V30Ah"),
    ("4824", "48V24Ah"), ("72V30", "72V30Ah"),
    ("6030Ah", "60V30Ah"), ("60-30安", "60V30Ah"),
])
def test_model_grammar_shared_by_product_and_semantics(query: str, canonical: str) -> None:
    assert model_selections(query)[0][1] == canonical
    assert analyze_customer_turn(query).battery_model == canonical
    assert analyze_customer_turn(query).money_offer is None


@pytest.mark.parametrize("query", [
    "500元", "30公里", "30W", "60300", "6030元", "60V30元", "60V30W", "60V30公里",
    "60V30.5", "订单号160300", "2026/10/08", "2026-10-08", "型号abc60V30xyz",
])
def test_other_numeric_types_do_not_become_battery_models(query: str) -> None:
    assert not model_selections(query)


@pytest.mark.parametrize("query", ["60V30便宜点", "60V30拍下了"])
def test_model_capacity_never_becomes_a_money_offer(query: str) -> None:
    semantics = analyze_customer_turn(query)
    assert semantics.battery_model == "60V30Ah"
    assert semantics.money_offer is None
    guarded = parse_and_guard_model_semantics(json.dumps({"money_offer": "30",
        "is_negotiation": True, "confidence": 1}), customer_text=query,
        deterministic=semantics, negotiation_active=True)
    assert guarded.money_offer is None
    plan = build_negotiation_plan(query, CURRENT_PRODUCTS[0], quantity=1,
                                  opening_counter="668.00", previous=None)
    assert plan is None or plan.customer_offer is None


def test_true_offer_and_multiple_models_remain_distinct() -> None:
    assert analyze_customer_turn("60V30 500行吗").money_offer == "500"
    assert model_selections("60V30/48V30") == (("6030", "60V30Ah"), ("4830", "48V30Ah"))
    match = resolve_products(CURRENT_PRODUCTS, "72V30", ("6030",))
    assert not match.products and match.source == "current_customer_model"


def test_adjacent_visible_clarification_continues_range() -> None:
    current = message("selection", "60V30")
    snap = snapshot(message("question", "能跑多远啊"),
                    message("clarification", "你问的是哪个型号的电池？", Direction.OUTGOING), current)
    question = resolve_reply_question(snap, (current,), current.text)
    assert question.inherited and question.source_message_keys == ("question",)
    answer = critical_answer(question.fact_query, resolve_products(CURRENT_PRODUCTS, question.fact_query))
    assert answer and "50-60公里" in answer.text and "单人平路" in answer.text


@pytest.mark.parametrize("new_query", [
    "60V30多少钱", "60V30 500行吗", "60V30拍下了", "不是6030换6020", "60V30尺寸多大",
    "60V30为什么这么贵", "60V30还没回答我", "收到", "你问的是哪个型号的电池？",
])
def test_new_question_or_order_does_not_inherit_old_attribute(new_query: str) -> None:
    current = message("new", new_query)
    snap = snapshot(message("old", "能跑多远"),
                    message("clarify", "哪个型号", Direction.OUTGOING), current)
    resolved = resolve_reply_question(snap, (current,), new_query)
    assert not resolved.inherited and resolved.fact_query == new_query


@pytest.mark.parametrize("middle", [
    message("answer", "二轮原装车单人平路续航50-60公里左右", Direction.OUTGOING),
    message("unknown", "哪个型号", Direction.UNKNOWN),
    message("manual", "我来处理", Direction.OUTGOING),
])
def test_answer_or_manual_intervention_closes_visible_question(middle: ChatMessage) -> None:
    current = message("new", "60V30")
    resolved = resolve_reply_question(snapshot(message("old", "能跑多远"), middle, current),
                                      (current,), current.text)
    assert not resolved.inherited


def test_guard_rejects_reasking_known_model_but_preserves_real_missing_details() -> None:
    question = ReplyQuestion("60V30", "60V30", ("new",))
    match = ProductMatch((CURRENT_PRODUCTS[0],), "current_customer_model")
    for text in ("你要哪个型号 发我看看", "你问的是什么规格", "电池型号是什么？"):
        with pytest.raises(FactGuardError, match="再次询问"):
            validate_question_reply(text, question, match, {})
    validate_question_reply("6030有不同版本，你要哪个版本？", question, match, {})
    validate_question_reply("这个接口需要什么规格？发接口照片看看", question, match, {})
    with pytest.raises(FactGuardError, match="再次询问"):
        validate_question_reply("你问的是哪个型号？", ReplyQuestion("72V30", "72V30", ("new",)),
                                ProductMatch((), "current_customer_model"), {})


class Clock:
    def __init__(self) -> None:
        self.time = datetime(2026, 10, 8, 10, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.time

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.time += timedelta(seconds=seconds)
        self.elapsed += seconds


class OfflinePage:
    """Satisfy real submission evidence, never connect to a browser."""
    def __init__(self, snap: ConversationSnapshot) -> None:
        self.snapshot = snap
        self.sent: list[str] = []
        self.checkpoint = None
        self.evidence: dict = {}

    def open_dedicated_chat_page(self) -> None:
        pass

    def check_page_health(self) -> PageHealth:
        return PageHealth(PageHealthStatus.HEALTHY)

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        return [ConversationSummary("chat", has_unread=False)][:limit]

    def open_conversation(self, key: str) -> None:
        assert key == self.snapshot.conversation_key

    def read_conversation(self) -> ConversationSnapshot:
        return self.snapshot

    def set_send_checkpoint(self, callback) -> None:
        self.checkpoint = callback

    def send_text(self, text: str) -> SendReceipt:
        snap = self.snapshot
        self.evidence = {"conversation_key": snap.conversation_key,
            "product_id": snap.platform_product_id, "merchant_account_id": snap.merchant_account_id,
            "platform_customer_id": snap.platform_customer_id,
            "last_customer_key": snap.last_incoming_message.message_key,
            "submitted_at": "2026-10-08T18:00:00+08:00", "click_state": "not_started",
            "baseline_keys": [m.message_key for m in snap.messages],
            "contact_message_evidence": [{"message_key": m.message_key,
                "direction": m.direction.value, "role_source": m.role_source, "text": m.text}
                for m in snap.messages]}
        assert self.checkpoint and self.checkpoint(self.evidence)
        self.sent.append(text)
        key = f"outgoing-{len(self.sent)}"
        self.snapshot = replace(snap, messages=snap.messages + (message(key, text, Direction.OUTGOING),))
        self.evidence["click_state"] = "click_returned"
        return SendReceipt(key, text)

    def send_evidence(self) -> dict:
        return self.evidence

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        return self.snapshot.last_message.message_key == receipt.message_key

    def verified_outgoing_message_key(self, receipt: SendReceipt) -> str:
        return receipt.message_key


class ForbiddenModel:
    def complete_text(self, **kwargs):
        raise AssertionError("The pending attribute must be answered from saved product facts")


def worker(page: OfflinePage, repo: CustomerServiceRepository, clock: Clock,
           mode: ReceptionMode = ReceptionMode.AUTO_SEND) -> CustomerServiceWorker:
    return CustomerServiceWorker(page, repo, ForbiddenModel(), clock=clock,
                                 config=CustomerServiceConfig(mode=mode))


def poll_reply(service: CustomerServiceWorker, clock: Clock) -> None:
    service.run_once()
    clock.advance(7)
    service.run_once()


@pytest.fixture
def repository(tmp_path: Path) -> CustomerServiceRepository:
    repo = CustomerServiceRepository(tmp_path / "regression.db")
    repo.initialize(migrate_confirmed_facts=False)
    for product in CURRENT_PRODUCTS:
        repo.save_product_knowledge(product)
    return repo


@pytest.mark.parametrize("restart", [False, True])
def test_screenshot_turns_answer_range_once_even_when_read_or_restarted(
    repository: CustomerServiceRepository, restart: bool,
) -> None:
    page = OfflinePage(snapshot(message("question", "能跑多远啊")))
    clock = Clock()
    service = worker(page, repository, clock)
    try:
        assert service.start() is ReceptionStatus.RUNNING
        poll_reply(service, clock)
        assert page.sent == ["你问的是哪个型号的电池？"]
        pending = repository.load_pending_reply_question("chat", exclude_turn_id="new")
        assert pending["missing_fields"] == ["battery_model"]
        if restart:
            service.stop()
            service.close()
            repository = CustomerServiceRepository(repository.database_path)
            service = worker(page, repository, clock)
            service.start()
            # Original question is virtualized away. The verified clarification
            # key and recorded source turn still authorize attribute continuation.
            page.snapshot = replace(page.snapshot, messages=page.snapshot.messages[-1:])
        page.snapshot = replace(page.snapshot,
                                messages=page.snapshot.messages + (message("selection", "60V30"),))
        poll_reply(service, clock)
        assert len(page.sent) == 2
        assert "50-60公里" in page.sent[-1] and "单人平路" in page.sent[-1]
        assert "哪个型号" not in page.sent[-1]
        assert not service.price_changes.list()
        for _ in range(3):
            poll_reply(service, clock)
        assert len(page.sent) == 2
        assert repository.load_pending_reply_question("chat", exclude_turn_id="new")["missing_fields"] == []
        with repository._connection() as db:
            rows = db.execute("SELECT decision_json FROM cs_customer_turns ORDER BY conversation_version").fetchall()
            final = json.loads(rows[-1][0])["reply_question"]
            assert final["source_message_keys"] == ["question"]
            assert final["clarification_message_key"] == "outgoing-1"
            assert final["requested_fields"] == ["range"]
            assert db.execute("SELECT count(*) FROM cs_submission_tombstones WHERE permission_issued=1").fetchone()[0] == 2
    finally:
        service.close()


def test_human_edit_cannot_drop_the_unanswered_attribute(repository: CustomerServiceRepository) -> None:
    page = OfflinePage(snapshot(message("question", "能跑多远啊"),
        message("clarification", "你问的是哪个型号的电池？", Direction.OUTGOING),
        message("selection", "60V30")))
    clock = Clock()
    service = worker(page, repository, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        assert draft.status is ReplyJobStatus.AWAITING_REVIEW
        result = service.approve_draft(draft.job_id, "收到")
        assert not result.sent and not page.sent
        assert "尚未解决" in result.reason
    finally:
        service.close()


def test_pending_question_rejects_identity_product_and_clarification_changes() -> None:
    current = message("selection", "60V30")
    snap = snapshot(message("clarification", "你问的是哪个型号的电池？", Direction.OUTGOING), current)
    pending = ReplyQuestion("能跑多远", "能跑多远", ("question",)).as_data(snap, missing_model=True)
    pending.update(clarification_message_key="clarification", turn_id="source")
    for field in ("platform_product_id", "merchant_account_id", "platform_customer_id"):
        assert not resolve_reply_question(replace(snap, **{field: "changed"}), (current,), current.text, pending).inherited
    assert not resolve_reply_question(snap, (current,), current.text,
                                      {**pending, "clarification_message_key": "other"}).inherited


def test_export_anonymizes_question_evidence_with_same_message_refs() -> None:
    payload = {"current_message_keys": ["selection"], "source_message_keys": ["question"],
               "clarification_message_key": "clarification", "continuation_turn_id": "turn1"}
    exported = _diagnostic_refs(payload, lambda kind, value: f"{kind}:{value}")
    assert exported == {"current_message_keys": ["msg:selection"], "source_message_keys": ["msg:question"],
                       "clarification_message_key": "msg:clarification", "continuation_turn_id": "turn:turn1"}


def test_continuation_guard_accepts_fact_and_rejects_generic_acknowledgement() -> None:
    question = ReplyQuestion("6030", "60V30Ah 续航多少", ("new",), ("old",), "clarification")
    match = ProductMatch((CURRENT_PRODUCTS[0],), "current_customer_model")
    answer = critical_answer(question.fact_query, match)
    check = validate_reply(answer.text, CURRENT_PRODUCTS, selected=match.products, query=question.fact_query)
    validate_question_reply(answer.text, question, match, check)
    with pytest.raises(FactGuardError, match="尚未解决"):
        validate_question_reply("收到", question, match, {})


@pytest.mark.parametrize("change", ["unverified", "uncertain_turn", "human_owned", "later_decision"])
def test_persisted_question_requires_one_verified_send_and_no_later_decision(
    repository: CustomerServiceRepository, change: str,
) -> None:
    page = OfflinePage(snapshot(message("question", "能跑多远啊")))
    clock = Clock()
    service = worker(page, repository, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert repository.load_pending_reply_question("chat", exclude_turn_id="new")["missing_fields"]
        with repository._connection() as db:
            if change == "unverified":
                db.execute("UPDATE cs_quality_sends SET status='uncertain' WHERE conversation_key='chat'")
            elif change == "uncertain_turn":
                db.execute("UPDATE cs_customer_turns SET status='verification_pending'")
            elif change == "human_owned":
                db.execute("UPDATE cs_conversation_states SET human_owned=1")
        if change == "later_decision":
            repository.observe_customer_turn(turn_id="later", conversation_key="chat",
                message_keys=("other",), customer_text="多少钱", platform_product_id="listing",
                observed_at=clock.now())
            repository.update_customer_turn("later", status="sent", decision_json="{}",
                                            updated_at=clock.now())
            assert repository.load_pending_reply_question("chat", exclude_turn_id="new")["missing_fields"] == []
        else:
            assert repository.load_pending_reply_question("chat", exclude_turn_id="new")["missing_fields"] == []
    finally:
        service.close()


@pytest.mark.parametrize("change", ["source_text", "manual_reply", "product_facts", "new_customer"])
def test_human_review_rechecks_source_and_current_facts_before_submission(
    repository: CustomerServiceRepository, change: str,
) -> None:
    page = OfflinePage(snapshot(message("question", "能跑多远啊"),
        message("clarification", "你问的是哪个型号的电池？", Direction.OUTGOING),
        message("selection", "60V30")))
    clock = Clock()
    service = worker(page, repository, clock, ReceptionMode.HUMAN_CONFIRMATION)
    try:
        service.start()
        poll_reply(service, clock)
        draft = service.drafts.list()[-1]
        if change == "source_text":
            page.snapshot = replace(page.snapshot, messages=(message("question", "实测容量多少"),
                                                              *page.snapshot.messages[1:]))
        elif change == "manual_reply":
            page.snapshot = replace(page.snapshot,
                messages=page.snapshot.messages + (message("manual", "我来处理", Direction.OUTGOING),))
        elif change == "new_customer":
            page.snapshot = replace(page.snapshot,
                messages=page.snapshot.messages + (message("new", "换6020"),))
        else:
            product = CURRENT_PRODUCTS[0]
            repository.save_product_knowledge(replace(product,
                specifications=product.specifications.replace("50-60公里", "45-55公里")))
        result = service.approve_draft(draft.job_id)
        assert not result.sent and not page.sent
    finally:
        service.close()


def test_stored_question_checks_visible_original_content() -> None:
    current = message("selection", "60V30")
    original = message("question", "能跑多远")
    clarify = message("clarification", "哪个型号", Direction.OUTGOING)
    snap = snapshot(original, clarify, current)
    pending = ReplyQuestion(original.text, original.text, (original.message_key,)).as_data(
        snap, missing_model=True)
    pending.update(clarification_message_key=clarify.message_key, turn_id="source")
    assert resolve_reply_question(snap, (current,), current.text, pending).inherited
    changed = replace(snap, messages=(replace(original, text="能跑一百公里吗"), clarify, current))
    assert not resolve_reply_question(changed, (current,), current.text, pending).inherited


@pytest.mark.parametrize("field,query,expected", [
    ("range", "能跑多远", "50-60公里"), ("dimensions", "尺寸多大", "17×18×32"),
    ("capacity", "实测容量多少", "29Ah"),
])
def test_other_attribute_questions_use_the_same_continuation(query: str, field: str, expected: str) -> None:
    current = message("selection", "60V30")
    resolved = resolve_reply_question(snapshot(message("question", query),
        message("clarify", "哪个型号", Direction.OUTGOING), current), (current,), current.text)
    match = resolve_products(CURRENT_PRODUCTS, resolved.fact_query)
    answer = critical_answer(resolved.fact_query, match)
    assert answer and expected in answer.text
    checked = validate_reply(answer.text, CURRENT_PRODUCTS, selected=match.products, query=resolved.fact_query)
    validate_question_reply(answer.text, resolved, match, checked)


@pytest.mark.parametrize("query,expected_price", [
    ("60V30多少钱", "678"), ("60V30我要加蓝牙多少钱", "698"),
    ("60V30 500行吗", "668"),
])
def test_price_bluetooth_quantity_and_offer_keep_raw_current_question(
    repository: CustomerServiceRepository, query: str, expected_price: str,
) -> None:
    page = OfflinePage(snapshot(message("old", "能跑多远"),
        message("clarify", "哪个型号", Direction.OUTGOING), message("current", query)))
    clock = Clock()
    service = worker(page, repository, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert len(page.sent) == 1 and expected_price in page.sent[0]
        assert "50-60公里" not in page.sent[0]
        assert not service.price_changes.list()
        with repository._connection() as db:
            decision = json.loads(db.execute("SELECT decision_json FROM cs_customer_turns").fetchone()[0])
            assert decision["reply_question"]["raw_query"] == query
            assert not decision["reply_question"]["source_message_keys"]
    finally:
        service.close()


def test_quantity_total_inputs_remain_price_parameters() -> None:
    semantics = analyze_customer_turn("60V30两组多少钱")
    quantity = quantity_from_messages("60V30两组多少钱", ())
    assert quantity == 2 and semantics.money_offer is None
    assert "1356" in _trusted_quantity_totals(CURRENT_PRODUCTS[0], quantity)


def test_visible_legacy_question_is_redacted_before_prompt_and_decision(
    repository: CustomerServiceRepository,
) -> None:
    page = OfflinePage(snapshot(message("question", "能跑多远？电话13800138000"),
        message("clarify", "哪个型号", Direction.OUTGOING), message("selection", "60V30")))
    clock = Clock()
    service = worker(page, repository, clock)
    try:
        service.start()
        poll_reply(service, clock)
        assert "50-60公里" in page.sent[-1]
        with repository._connection() as db:
            decision = db.execute("SELECT decision_json FROM cs_customer_turns").fetchone()[0]
            assert "13800138000" not in decision
            assert "手机号" in decision
    finally:
        service.close()

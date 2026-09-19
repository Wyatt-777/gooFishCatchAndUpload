"""CS-4 tests for scheduling, debounce, drafting, and human approval."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    FIRST_CONTACT_CATALOG_REPLY,
)
from xianyu_assistant.customer_service.customer_service_worker import (
    CustomerServiceWorker,
    _current_operating_policy_reply,
    parse_reply_proposal,
)
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    CustomerServiceConfig,
    HistoricalExample,
    MessageDirection,
    MessageKind,
    ModelResponse,
    PageHealth,
    PageHealthStatus,
    PriceChangeDraft,
    PriceChangeReceipt,
    PriceChangeStatus,
    ProductKnowledge,
    ReceptionMode,
    ReceptionStatus,
    ReplyDraft,
    ReplyJobStatus,
    SalesStage,
    SalesState,
    SendReceipt,
)
from xianyu_assistant.customer_service.negotiation import NegotiationState
from xianyu_assistant.customer_service.protocols import (
    PriceChangeNotPerformedError,
    TextSendNotPerformedError,
)


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


class FakeAdapter:
    def __init__(self, snapshots: dict[str, ConversationSnapshot]) -> None:
        self.snapshots = snapshots
        self.summaries = [ConversationSummary(key) for key in snapshots]
        self.opened: list[str] = []
        self.sent: list[str] = []
        self.page_opened = 0
        self.health = PageHealth(PageHealthStatus.HEALTHY)
        self.changed_prices: list[str] = []

    def open_dedicated_chat_page(self) -> None:
        self.page_opened += 1

    def check_page_health(self) -> PageHealth:
        return self.health

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        return self.summaries

    def open_conversation(self, conversation_key: str) -> None:
        self.opened.append(conversation_key)

    def read_conversation(self) -> ConversationSnapshot:
        return self.snapshots[self.opened[-1]]

    def request_voice_transcript(self, message_key: str) -> str | None:
        return "语音转写内容"

    def capture_incoming_image(self, message_key: str):
        raise AssertionError("worker test does not need vision input")

    def send_text(self, text: str) -> SendReceipt:
        self.sent.append(text)
        return SendReceipt(f"sent-{len(self.sent)}", text)

    def send_approved_image(self, path: Path) -> SendReceipt:
        raise AssertionError("CS-4 must not send images")

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        return True

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        self.changed_prices.append(approved_price)
        return PriceChangeReceipt("5.00", approved_price)


class RetryableSendAdapter(FakeAdapter):
    def send_text(self, text: str) -> SendReceipt:
        del text
        raise TextSendNotPerformedError("点击未生效，草稿可再次人工确认。")


class FailingSendAdapter(FakeAdapter):
    def send_text(self, text: str) -> SendReceipt:
        del text
        raise RuntimeError("send failed")


class AutoPriceAdapter(FakeAdapter):
    def __init__(self, snapshots: dict[str, ConversationSnapshot]) -> None:
        super().__init__(snapshots)
        self.events: list[str] = []

    def send_text(self, text: str) -> SendReceipt:
        self.events.append("send")
        return super().send_text(text)

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        self.events.append("price_change")
        return super().change_order_price(approved_price)


class AutoPriceNotPerformedAdapter(AutoPriceAdapter):
    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        del approved_price
        self.events.append("price_change")
        raise PriceChangeNotPerformedError("最终确认按钮不可用，未改价。")


class FakeRepository:
    def __init__(self) -> None:
        self.saved = []
        self.processed: set[str] = set()
        self.handoffs: list[tuple[str, str]] = []
        self.knowledge_queries: list[str] = []
        self.product = ProductKnowledge(
            product_key="p1",
            name="测试商品",
            platform_product_id="p1",
            listed_price="99",
            minimum_price="80",
            specifications="黑色，标准版",
        )
        self.curated_answers = [
            HistoricalExample(
                "current-test-6030-size",
                "6030尺寸多大",
                "6030尺寸17-18-32",
                "user_curated",
            )
        ]
        self.price_changes: list[PriceChangeDraft] = []
        self.negotiation_states: dict[str, tuple[NegotiationState, str]] = {}
        self.sales_states: dict[str, SalesState] = {}

    def find_product_knowledge(self, *, platform_product_id=None, normalized_title=None):
        return self.product

    def find_product_knowledge_for_query(self, query: str):
        del query
        return self.product

    def get_product_knowledge(self, product_key: str):
        if self.product is not None and self.product.product_key == product_key:
            return self.product
        return None

    def find_historical_examples(self, query: str, limit: int):
        return [HistoricalExample("h1", "多少钱", "您好，价格以页面为准。", "human_confirmed")]

    def find_curated_knowledge_answers(self, query: str, limit: int):
        return self.curated_answers[:limit]

    def find_knowledge_answers(self, query: str, limit: int):
        self.knowledge_queries.append(query)
        return self.curated_answers[:limit]

    def has_prior_reply_job(self, conversation_key: str, *, exclude_job_id: str) -> bool:
        return any(
            getattr(item, "conversation_key", None) == conversation_key
            and getattr(item, "job_id", None) != exclude_job_id
            for item in self.saved
        )

    def save_negotiation_state(
        self,
        conversation_key: str,
        state: NegotiationState,
        *,
        price_variant: str,
    ) -> None:
        self.negotiation_states[conversation_key] = (state, price_variant)

    def load_negotiation_state(
        self, conversation_key: str
    ) -> tuple[NegotiationState, str] | None:
        return self.negotiation_states.get(conversation_key)

    def save_sales_state(self, state: SalesState) -> None:
        self.sales_states[state.conversation_key] = state

    def cancel_sales_follow_up(self, conversation_key: str, *, updated_at: datetime) -> None:
        state = self.sales_states.get(conversation_key)
        if state is not None and state.status == "pending":
            self.sales_states[conversation_key] = SalesState(
                conversation_key=state.conversation_key,
                product_key=state.product_key,
                stage=state.stage,
                follow_up_count=state.follow_up_count,
                last_customer_message_key=state.last_customer_message_key,
                last_merchant_fingerprint=state.last_merchant_fingerprint,
                status="cancelled",
                updated_at=updated_at,
            )

    def list_due_sales_follow_ups(self, *, now: datetime, limit: int):
        return [
            state
            for state in self.sales_states.values()
            if state.status == "pending"
            and state.follow_up_due_at is not None
            and state.follow_up_due_at <= now
        ][:limit]

    def mark_sales_follow_up_sent(
        self,
        conversation_key: str,
        *,
        merchant_fingerprint: str,
        updated_at: datetime,
    ) -> None:
        state = self.sales_states[conversation_key]
        self.sales_states[conversation_key] = SalesState(
            conversation_key=state.conversation_key,
            product_key=state.product_key,
            stage=SalesStage.FOLLOWED_UP,
            follow_up_count=state.follow_up_count + 1,
            last_customer_message_key=state.last_customer_message_key,
            last_merchant_fingerprint=merchant_fingerprint,
            status="followed_up",
            updated_at=updated_at,
        )

    def has_import_batch(self, source_sha256: str) -> bool:
        return False

    def store_import(self, bundle: object) -> bool:
        return True

    def get_media_asset(self, asset_id: str):
        return None

    def save_reply_draft(self, draft: object) -> None:
        self.saved.append(draft)

    def save_price_change_draft(self, draft: PriceChangeDraft) -> None:
        self.price_changes = [item for item in self.price_changes if item.task_id != draft.task_id]
        self.price_changes.append(draft)

    def list_price_change_drafts(self, limit: int = 100):
        return self.price_changes[-limit:]

    def find_reply_draft(self, *, conversation_key: str, batch_fingerprint: str):
        for item in reversed(self.saved):
            if (
                getattr(item, "conversation_key", None) == conversation_key
                and getattr(item, "batch_fingerprint", None) == batch_fingerprint
            ):
                return item
        return None

    def has_processed_fingerprint(self, batch_fingerprint: str) -> bool:
        return batch_fingerprint in self.processed

    def add_processed_fingerprint(self, batch_fingerprint: str, observed_at: datetime) -> None:
        self.processed.add(batch_fingerprint)

    def save_human_confirmed_example(self, example: HistoricalExample) -> None:
        self.saved.append(example)

    def save_handoff_event(self, *, conversation_key: str, reason: str, created_at: datetime) -> None:
        del created_at
        if not any(key == conversation_key for key, _reason in self.handoffs):
            self.handoffs.append((conversation_key, reason))

    def has_open_handoff(self, conversation_key: str) -> bool:
        return any(key == conversation_key for key, _reason in self.handoffs)


class FirstContactRepository(FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.first_contact: dict[str, tuple[str, str]] = {}

    def should_send_first_contact_catalog(
        self, conversation_key: str, *, snapshot_has_outgoing: bool
    ) -> bool:
        return not snapshot_has_outgoing and conversation_key not in self.first_contact

    def reserve_first_contact_catalog(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        reply_fingerprint: str,
        created_at: datetime,
    ) -> bool:
        del reply_fingerprint, created_at
        if conversation_key in self.first_contact:
            return False
        self.first_contact[conversation_key] = (reservation_id, "reserved")
        return True

    def release_first_contact_catalog_reservation(
        self, conversation_key: str, *, reservation_id: str
    ) -> None:
        if self.first_contact.get(conversation_key) == (reservation_id, "reserved"):
            self.first_contact.pop(conversation_key)

    def mark_first_contact_catalog_sent(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        updated_at: datetime,
    ) -> None:
        del updated_at
        if self.first_contact.get(conversation_key) == (reservation_id, "reserved"):
            self.first_contact[conversation_key] = (reservation_id, "sent")


class FakeModel:
    def __init__(self, reply: str = "您好，测试商品目前按页面价格出售。") -> None:
        self.calls = 0
        self.reply = reply
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None

    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return ModelResponse(json.dumps({"reply_text": self.reply}, ensure_ascii=False))

    def complete_with_image(self, *, system_prompt: str, user_prompt: str, image, model: str):
        self.calls += 1
        return ModelResponse(json.dumps({"reply_text": self.reply}, ensure_ascii=False))


class FailingModel(FakeModel):
    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        raise RuntimeError("temporary model failure")


class PriceModel(FakeModel):
    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return ModelResponse(
            json.dumps(
                {
                    "reply_text": "行 给你改到85",
                    "offered_price": "85",
                    "intent": "order_price_change",
                },
                ensure_ascii=False,
            )
        )


class BluetoothPriceModel(FakeModel):
    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return ModelResponse(
            json.dumps(
                {
                    "reply_text": "400可以 给你改价",
                    "offered_price": "400",
                    "intent": "order_price_change",
                },
                ensure_ascii=False,
            )
        )


class SemanticAwareModel(FakeModel):
    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        if "语义解析器" in system_prompt:
            return ModelResponse(
                json.dumps(
                    {
                        "primary_intent": "compatibility",
                        "money_offer": None,
                        "is_negotiation": False,
                        "ambiguities": [],
                        "confidence": 0.96,
                        "evidence": [{"text": "300", "type": "unknown_parameter"}],
                    },
                    ensure_ascii=False,
                )
            )
        return ModelResponse(
            json.dumps({"reply_text": "这个要结合车型确认"}, ensure_ascii=False)
        )


def _snapshot(key: str, *texts: str) -> ConversationSnapshot:
    messages = tuple(
        ChatMessage(f"{key}-m{index}", MessageDirection.INCOMING, MessageKind.TEXT, text)
        for index, text in enumerate(texts, 1)
    )
    return ConversationSnapshot(key, messages, platform_product_id="p1", product_title="测试商品")


def _dialog_snapshot(
    key: str,
    *turns: tuple[MessageDirection, str],
) -> ConversationSnapshot:
    messages = tuple(
        ChatMessage(f"{key}-m{index}", direction, MessageKind.TEXT, text)
        for index, (direction, text) in enumerate(turns, 1)
    )
    return ConversationSnapshot(
        key,
        messages,
        platform_product_id="p1",
        product_title="测试商品",
    )


def _worker(adapter: FakeAdapter, clock: FakeClock, repository: FakeRepository, model: FakeModel):
    return CustomerServiceWorker(
        adapter,
        repository,
        model,
        config=CustomerServiceConfig(),
        clock=clock,
    )


def test_current_sales_business_rules_are_deterministic() -> None:
    assert _current_operating_policy_reply("送充电器吗").reply_text == "默认送充电器"
    assert _current_operating_policy_reply("海南包邮吗").reply_text == "海南不发货"
    assert _current_operating_policy_reply("新疆包邮吗").reply_text == "新疆可以发 但不包邮"
    assert _current_operating_policy_reply("发海南吗").reply_text == "海南不发货"
    assert (
        _current_operating_policy_reply("质保多久，容量虚标怎么办").reply_text
        == "所有电池质保一年 容量虚标包退"
    )
    video = _current_operating_policy_reply("发货前能看容量测试视频吗")
    assert video.requires_handoff is True
    assert "容量测试视频" in (video.handoff_reason or "")


def test_auto_sales_follow_up_is_persisted_and_sent_once_after_30_minutes() -> None:
    clock = FakeClock()
    clock.wall = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    adapter = FakeAdapter({"c1": _snapshot("c1", "这个电池质量怎么样")})
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FakeModel(),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    first_sent = adapter.sent[-1]
    pending = repository.sales_states["c1"]
    assert pending.status == "pending"
    assert pending.follow_up_count == 0

    adapter.snapshots["c1"] = _dialog_snapshot(
        "c1",
        (MessageDirection.INCOMING, "这个电池质量怎么样"),
        (MessageDirection.OUTGOING, first_sent),
    )
    last = adapter.snapshots["c1"].messages[-1]
    adapter.snapshots["c1"] = ConversationSnapshot(
        "c1",
        (
            adapter.snapshots["c1"].messages[0],
            ChatMessage(
                last.message_key,
                last.direction,
                last.kind,
                last.text,
                content_fingerprint=first_sent,
            ),
        ),
        platform_product_id="p1",
        product_title="测试商品",
    )
    adapter.summaries = []
    clock.advance(30 * 60)
    worker.run_once()

    assert len(adapter.sent) == 2
    assert "二轮还是三轮" in adapter.sent[-1]
    assert repository.sales_states["c1"].status == "followed_up"
    assert repository.sales_states["c1"].follow_up_count == 1

    clock.advance(30 * 60)
    worker.run_once()
    assert len(adapter.sent) == 2


def test_due_sales_follow_up_is_cancelled_if_customer_has_replied() -> None:
    clock = FakeClock()
    clock.wall = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    adapter = FakeAdapter({"c1": _snapshot("c1", "这个电池质量怎么样")})
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FakeModel(),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    first_sent = adapter.sent[-1]

    adapter.snapshots["c1"] = _dialog_snapshot(
        "c1",
        (MessageDirection.INCOMING, "这个电池质量怎么样"),
        (MessageDirection.OUTGOING, first_sent),
        (MessageDirection.INCOMING, "我是二轮车"),
    )
    adapter.summaries = []
    clock.advance(30 * 60)
    worker.run_once()

    assert adapter.sent == [first_sent]
    assert repository.sales_states["c1"].status == "cancelled"


def test_worker_debounces_then_creates_human_review_draft() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030尺寸能装进车里吗？")})
    repository = FakeRepository()
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    assert worker.start().value == "running"
    assert worker.run_once() == 1
    assert model.calls == 0
    assert worker.drafts.list()[0].status is ReplyJobStatus.DEBOUNCING

    clock.advance(2)
    worker.run_once()
    assert model.calls == 0
    clock.advance(4)
    worker.run_once()

    drafts = worker.drafts.list()
    assert model.calls == 1
    assert drafts[-1].status is ReplyJobStatus.AWAITING_REVIEW
    assert adapter.sent == []


def test_first_customer_conversation_prepends_catalog_once_across_worker_restart() -> None:
    clock = FakeClock()
    repository = FirstContactRepository()
    first_adapter = FakeAdapter({"c1": _snapshot("c1", "6030能跑多远")})
    first_worker = CustomerServiceWorker(
        first_adapter,
        repository,
        FakeModel("这个要结合车型确认"),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    first_worker.start()
    first_worker.run_once()
    clock.advance(10)
    first_worker.run_once()

    assert len(first_adapter.sent) == 1
    assert first_adapter.sent[0].startswith(FIRST_CONTACT_CATALOG_REPLY)
    assert repository.first_contact["c1"][1] == "sent"

    second_snapshot = ConversationSnapshot(
        "c1",
        (
            ChatMessage(
                "c1-m2",
                MessageDirection.INCOMING,
                MessageKind.TEXT,
                "6030能装吗",
            ),
        ),
        platform_product_id="p1",
        product_title="测试商品",
    )
    second_adapter = FakeAdapter({"c1": second_snapshot})
    second_worker = CustomerServiceWorker(
        second_adapter,
        repository,
        FakeModel("要看尺寸能不能放下"),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )
    second_worker.start()
    second_worker.run_once()
    clock.advance(10)
    second_worker.run_once()

    assert len(second_adapter.sent) == 1
    assert second_adapter.sent[0].startswith("要看尺寸能不能放下")
    assert not second_adapter.sent[0].startswith(FIRST_CONTACT_CATALOG_REPLY)


def test_existing_merchant_message_disables_first_contact_catalog() -> None:
    clock = FakeClock()
    repository = FirstContactRepository()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.OUTGOING, "之前已经聊过"),
                (MessageDirection.INCOMING, "6030能装吗"),
            )
        }
    )
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FakeModel("要看尺寸能不能放下"),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert len(adapter.sent) == 1
    assert adapter.sent[0].startswith("要看尺寸能不能放下")
    assert not adapter.sent[0].startswith(FIRST_CONTACT_CATALOG_REPLY)


def test_first_observation_keeps_the_complete_customer_turn() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030 2000W", "带得动不")})
    repository = FakeRepository()
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["current_customer_query"] == "6030 2000W 带得动不"
    assert prompt["customer_semantics"]["entities"]["motor_power_w"] == "2000"


def test_ambiguous_numeric_turn_uses_model_semantics_before_reply_generation() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030 300能不能")})
    repository = FakeRepository()
    model = SemanticAwareModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.calls == 2
    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["customer_semantics"]["primary_intent"] == "compatibility"
    assert prompt["customer_semantics"]["entities"]["money_offer"] is None


def test_non_negotiation_range_number_cannot_be_rendered_as_rejected_offer() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "60伏20安，25公里的多少钱")})
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已介绍商品",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("25不行 398可以")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.calls == CustomerServiceConfig().max_model_attempts
    assert worker.drafts.list()[-1].status is ReplyJobStatus.FAILED
    assert adapter.sent == []


def test_restarted_worker_restores_accepted_price_for_purchase_howto() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "怎么拍下")})
    repository = FakeRepository()
    repository.negotiation_states["c1"] = (
        NegotiationState(
            product_key="p1",
            quantity=1,
            last_customer_offer="85",
            accepted_price="85",
            outcome="accept",
        ),
        "base",
    )
    model = FakeModel("直接拍下就行 还是按85")
    restarted_worker = _worker(adapter, clock, repository, model)

    restarted_worker.start()
    restarted_worker.run_once()
    clock.advance(10)
    restarted_worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["negotiation_decision"]["outcome"] == "accept"
    assert prompt["negotiation_decision"]["accepted_price"] == "85"
    worker_draft = restarted_worker.drafts.list()[-1]
    assert worker_draft.reply_text == "直接拍下就行 还是按85"
    assert repository.price_changes == []


def test_order_price_change_is_queued_then_requires_separate_confirmation() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6020我已拍下待付款，按85改价")})
    repository = FakeRepository()
    model = PriceModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    task = worker.price_changes.list()[0]
    assert task.status is PriceChangeStatus.AWAITING_REVIEW
    assert task.proposed_price == "85.00"
    assert adapter.changed_prices == []

    future = worker.enqueue_price_change(task.task_id)
    worker.run_once()

    result = future.result()
    assert result.applied is True
    assert result.status is PriceChangeStatus.APPLIED
    assert adapter.changed_prices == ["85.00"]


def test_bluetooth_option_question_uses_base_price_plus_twenty() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6020加蓝牙多少钱？")})
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    model = FakeModel("不应调用模型")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.reply_text == "蓝牙自己选装 加20元 加装后418元"
    assert draft.status is ReplyJobStatus.AWAITING_REVIEW
    assert repository.handoffs == []
    assert model.calls == 0


def test_bluetooth_selection_adjusts_negotiation_facts_and_floor() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6020我要加蓝牙，400可以吗")})
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    model = FakeModel("400可以")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["product"]["listed_price"] == "418.00"
    assert prompt["negotiation_decision"]["outcome"] == "accept"
    assert prompt["negotiation_decision"]["accepted_price"] == "400"
    assert prompt["customer_memory"][-1] == (
        "顾客已选择加装蓝牙模块，商品价格在基础价格上增加20元"
    )
    assert worker.drafts.list()[-1].reply_text == "400可以"


def test_bluetooth_cancellation_restores_base_catalog_price() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "6020我要加蓝牙"),
                (MessageDirection.OUTGOING, "可以 加20"),
                (MessageDirection.INCOMING, "不用蓝牙了，380可以吗"),
            )
        }
    )
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    model = FakeModel("380可以")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["product"]["listed_price"] == "398.00"
    assert prompt["negotiation_decision"]["outcome"] == "accept"
    assert all("蓝牙模块" not in item for item in prompt["customer_memory"])
    assert worker.drafts.list()[-1].reply_text == "380可以"


def test_short_confirmation_after_bluetooth_question_selects_option() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "6020蓝牙多少钱"),
                (MessageDirection.OUTGOING, "蓝牙自己选装 加20元"),
                (MessageDirection.INCOMING, "要"),
                (MessageDirection.INCOMING, "400可以吗"),
            )
        }
    )
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    model = FakeModel("400可以")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["product"]["listed_price"] == "418.00"
    assert prompt["negotiation_decision"]["outcome"] == "accept"


def test_bluetooth_order_price_change_keeps_option_surcharge_through_execution() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {"c1": _snapshot("c1", "6020我要加蓝牙，已拍了，按400改价")}
    )
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v20ah"
    )
    worker = _worker(adapter, clock, repository, BluetoothPriceModel())

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    task = worker.price_changes.list()[0]
    assert task.status is PriceChangeStatus.AWAITING_REVIEW
    assert task.product_key == "current-tieta-60v20ah"
    assert task.product_name.endswith("+ 蓝牙模块")
    assert task.proposed_price == "400.00"
    assert task.minimum_price == "398.00"
    assert task.listed_price == "418.00"
    assert task.price_adjustment_key == "bluetooth_module"
    assert task.price_adjustment_amount == "20.00"

    future = worker.enqueue_price_change(task.task_id)
    worker.run_once()

    result = future.result()
    assert result.applied is True
    assert result.status is PriceChangeStatus.APPLIED
    assert adapter.changed_prices == ["400.00"]


def test_auto_mode_applies_guarded_price_change_before_sending_reply() -> None:
    clock = FakeClock()
    adapter = AutoPriceAdapter(
        {"c1": _snapshot("c1", "6020我已拍下待付款，按85改价")}
    )
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        PriceModel(),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert adapter.events == ["price_change", "send"]
    assert adapter.changed_prices == ["85.00"]
    assert adapter.sent == ["行 给你改到85"]
    assert worker.price_changes.list()[0].status is PriceChangeStatus.APPLIED
    assert worker.drafts.list()[-1].status is ReplyJobStatus.SENT


def test_auto_price_failure_hands_off_only_that_customer_without_sending_claim() -> None:
    clock = FakeClock()
    adapter = AutoPriceNotPerformedAdapter(
        {"c1": _snapshot("c1", "6020我已拍下待付款，按85改价")}
    )
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        PriceModel(),
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert adapter.events == ["price_change"]
    assert adapter.sent == []
    assert worker.price_changes.list()[0].status is PriceChangeStatus.AWAITING_REVIEW
    assert worker.drafts.list()[-1].status is ReplyJobStatus.HANDOFF
    assert repository.handoffs == [("c1", "最终确认按钮不可用，未改价。")]
    assert worker.status is ReceptionStatus.RUNNING


def test_missing_product_creates_saved_catalog_without_model_call() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "你好，我想买电池")})
    repository = FakeRepository()
    repository.product = None
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.status is ReplyJobStatus.AWAITING_REVIEW
    assert draft.reply_text.startswith("原装25年铁塔")
    assert "60V30A 678元 尺寸17-18-32" in draft.reply_text
    assert model.calls == 0
    assert repository.handoffs == []
    assert adapter.sent == []


def test_partial_voltage_without_capacity_uses_saved_catalog() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "需要60V电池")})
    repository = FakeRepository()
    repository.product = None
    worker = _worker(adapter, clock, repository, FakeModel())

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.status is ReplyJobStatus.AWAITING_REVIEW
    assert draft.reply_text.startswith("原装25年铁塔")


def test_explicit_model_with_price_question_uses_saved_catalog() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030多少钱？")})
    repository = FakeRepository()
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.reply_text.startswith("原装25年铁塔")
    assert model.calls == 0


def test_explicit_model_with_colloquial_price_question_uses_saved_catalog() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "4830多少一个")})
    repository = FakeRepository()
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert worker.drafts.list()[-1].reply_text.startswith("原装25年铁塔")
    assert model.calls == 0


def test_explicit_model_without_price_question_uses_normal_model_flow() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030尺寸能装进车里吗？")})
    repository = FakeRepository()
    model = FakeModel("6030尺寸为17-18-32，请核对电池仓尺寸。")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.reply_text == "6030尺寸为17-18-32，请核对电池仓尺寸。"
    assert model.calls == 1


def test_model_prompt_enforces_observed_merchant_style_contract() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030尺寸能装进车里吗？")})
    repository = FakeRepository()
    model = FakeModel("可以的 你先量下电池仓")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.last_system_prompt is not None
    assert "必须最大限度模仿历史商家" in model.last_system_prompt
    assert "历史样例中的不友善表达不得模仿" in model.last_system_prompt
    assert model.last_user_prompt is not None
    prompt = json.loads(model.last_user_prompt)
    style_contract = prompt["merchant_style_contract"]
    assert any("普通回复优先控制在4到16个字" in rule for rule in style_contract)
    assert any("不用“您好”“亲”" in rule for rule in style_contract)
    assert any("一般不加句末标点" in rule for rule in style_contract)
    assert prompt["merchant_style_profile_version"] == "battery_seller_voice_v2_20260913"
    assert prompt["merchant_style_metrics"]["compact_character_median"] == 9
    assert any(
        "上一轮在问价格，顾客随后只发型号" in rule
        for rule in prompt["merchant_dialogue_playbook"]
    )
    assert prompt["knowledge_answers"] == [
        {
            "question": "6030尺寸多大",
            "answer": "6030尺寸17-18-32",
            "trust_level": "user_curated",
        }
    ]
    assert prompt["current_customer_query"] == "6030尺寸能装进车里吗？"
    assert "historical_style_examples" not in prompt


def test_follow_up_question_does_not_repeat_first_question_catalog() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "这个怎么卖？")})
    repository = FakeRepository()
    model = FakeModel("6030尺寸17-18-32")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    assert worker.drafts.list()[-1].reply_text.startswith("原装25年铁塔")
    assert model.calls == 0

    adapter.snapshots["c1"] = _snapshot("c1", "这个怎么卖？", "6030尺寸多大？")
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert worker.drafts.list()[-1].reply_text == "6030尺寸17-18-32"
    assert model.calls == 1
    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["current_customer_query"] == "6030尺寸多大？"


def test_confirmed_default_carriers_reply_without_handoff_or_model() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "默认发什么快递？")})
    repository = FakeRepository()
    model = FakeModel("不应调用模型")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    draft = worker.drafts.list()[-1]
    assert draft.reply_text == "默认发安能物流和京东"
    assert draft.status is ReplyJobStatus.AWAITING_REVIEW
    assert repository.handoffs == []
    assert model.calls == 0


def test_short_price_follow_up_reuses_latest_customer_model() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "怎么卖？"),
                (MessageDirection.OUTGOING, "你要哪个型号"),
                (MessageDirection.INCOMING, "6020"),
                (MessageDirection.OUTGOING, "6020尺寸14.5-17-29"),
                (MessageDirection.INCOMING, "多少钱？"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample("current-test-price", "60V20Ah多少钱", "60V20Ah 428元", "user_curated")
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("6020 428元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["current_customer_query"] == "多少钱？"
    assert prompt["resolved_customer_query"] == "6020 60V20Ah 多少钱"
    assert prompt["customer_memory"] == ["顾客最近确认的规格是6020，对应60V20Ah"]
    assert repository.knowledge_queries == ["6020 60V20Ah 多少钱"]
    assert "private_negotiation_floor" not in prompt["product"]
    assert "minimum_price" not in prompt["product"]
    assert worker.drafts.list()[-1].reply_text == "6020 428元"


def test_private_floor_is_only_exposed_to_model_for_explicit_negotiation() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "这个怎么卖"),
                (MessageDirection.OUTGOING, "标价99"),
                (MessageDirection.INCOMING, "80卖不卖"),
            )
        }
    )
    repository = FakeRepository()
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("80可以")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert "private_negotiation_floor" not in prompt["product"]
    assert prompt["negotiation_decision"]["outcome"] == "accept"
    assert prompt["negotiation_decision"]["accepted_price"] == "80"
    assert "不得主动透露内部底价" in prompt["price_fact_contract"]


def test_repeated_model_negotiation_reply_falls_back_to_fresh_wording() -> None:
    clock = FakeClock()
    repeated_reply = "628不行 668行的话直接拍"
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "628可以吗"),
                (MessageDirection.OUTGOING, repeated_reply),
                (MessageDirection.INCOMING, "为什么这么贵 别人都是628"),
            )
        }
    )
    repository = FakeRepository()
    repository.product = next(
        product
        for product in CURRENT_PRODUCTS
        if product.product_key == "current-tieta-60v30ah"
    )
    model = FakeModel(repeated_reply)
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.calls == CustomerServiceConfig().max_model_attempts
    assert worker.drafts.list()[-1].reply_text == "628不行 668可以"
    assert worker.drafts.list()[-1].reply_text != repeated_reply


def test_customer_model_answer_inherits_previous_price_intent() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "多少钱？"),
                (MessageDirection.OUTGOING, "你要哪个型号"),
                (MessageDirection.INCOMING, "6020"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample("current-test-price", "60V20Ah多少钱", "60V20Ah 428元", "user_curated")
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("6020 428元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["current_customer_query"] == "6020"
    assert prompt["resolved_customer_query"] == "6020 60V20Ah 多少钱"
    assert repository.knowledge_queries == ["6020 60V20Ah 多少钱"]
    assert worker.drafts.list()[-1].reply_text == "6020 428元"


def test_customer_correction_uses_last_model_in_current_message() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "6020多少钱？"),
                (MessageDirection.OUTGOING, "6020 428元"),
                (MessageDirection.INCOMING, "不是6020，换6030，多少钱？"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample("current-test-price", "60V30Ah多少钱", "60V30Ah 658元", "user_curated")
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("6030 658元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["resolved_customer_query"] == "6030 60V30Ah 多少钱"
    assert prompt["customer_memory"] == ["顾客最近确认的规格是6030，对应60V30Ah"]
    assert worker.drafts.list()[-1].reply_text == "6030 658元"


def test_split_voltage_and_capacity_are_merged_across_clarification_turn() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "60V的多少钱？"),
                (MessageDirection.OUTGOING, "要多大容量"),
                (MessageDirection.INCOMING, "30Ah"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample("current-test-price", "60V30Ah多少钱", "60V30Ah 658元", "user_curated")
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("60V30Ah 658元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["current_customer_query"] == "30Ah"
    assert prompt["resolved_customer_query"] == "60V30Ah 多少钱"
    assert prompt["customer_memory"] == ["顾客最近确认的规格是60V30Ah"]
    assert worker.drafts.list()[-1].reply_text == "60V30Ah 658元"


def test_compact_voltage_capacity_selection_is_remembered_for_followup() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "48伏30安有吗"),
                (MessageDirection.OUTGOING, "有的"),
                (MessageDirection.INCOMING, "多少钱"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample(
            "current-test-price",
            "48V30Ah多少钱",
            "48V30Ah 99元",
            "user_curated",
        )
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("48V30Ah 99元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["resolved_customer_query"] == "48V30Ah 多少钱"
    assert prompt["customer_memory"] == ["顾客最近确认的规格是48V30Ah"]


def test_multiple_models_in_current_question_are_all_kept() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "c1": _dialog_snapshot(
                "c1",
                (MessageDirection.INCOMING, "怎么卖？"),
                (MessageDirection.OUTGOING, "你要哪个型号"),
                (MessageDirection.INCOMING, "6020和6030分别多少钱？"),
            )
        }
    )
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample("current-test-price-6020", "60V20Ah多少钱", "60V20Ah 428元", "user_curated"),
        HistoricalExample("current-test-price-6030", "60V30Ah多少钱", "60V30Ah 658元", "user_curated"),
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="已回复",
            status=ReplyJobStatus.SENT,
        )
    )
    model = FakeModel("6020 428元 6030 658元")
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    prompt = json.loads(model.last_user_prompt or "{}")
    assert prompt["resolved_customer_query"] == (
        "6020 60V20Ah 6030 60V30Ah 多少钱"
    )
    assert prompt["customer_memory"] == [
        "顾客当前同时询问6020，对应60V20Ah",
        "顾客当前同时询问6030，对应60V30Ah",
    ]
    assert worker.drafts.list()[-1].reply_text == "6020 428元 6030 658元"


class RepairingPriceModel(FakeModel):
    def complete_text(self, *, system_prompt: str, user_prompt: str, model: str) -> ModelResponse:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        reply = (
            "6030尺寸17-18-32\n6030一组638 包邮\n具体价格看你要几组"
            if self.calls == 1
            else "先量下电池仓"
        )
        return ModelResponse(json.dumps({"reply_text": reply}, ensure_ascii=False))


def test_unsupported_old_price_is_rejected_and_rewritten_from_curated_fact() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "这个能装进车里吗？")})
    repository = FakeRepository()
    repository.curated_answers = [
        HistoricalExample(
            "current-test-install-fit",
            "能装进车里吗",
            "先量下电池仓",
            "user_curated",
        )
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="首问已处理",
            status=ReplyJobStatus.SUPERSEDED,
        )
    )
    model = RepairingPriceModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.calls == 2
    assert worker.drafts.list()[-1].reply_text == "先量下电池仓"
    assert "未获当前知识支持的数字" in (model.last_user_prompt or "")


def test_lower_trust_historical_number_cannot_override_curated_answer() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "这个能装进车里吗？")})
    repository = FakeRepository()
    repository.product = None
    repository.curated_answers = [
        HistoricalExample(
            "current-test-install-fit",
            "能装进车里吗",
            "先量下电池仓",
            "user_curated",
        ),
        HistoricalExample(
            "old-chat",
            "能装进车里吗",
            "60V30Ah 638元 包邮",
            "conversation_knowledge",
        ),
    ]
    repository.saved.append(
        ReplyDraft(
            job_id="earlier-job",
            conversation_key="c1",
            batch_fingerprint="earlier-batch",
            reply_text="首问已处理",
            status=ReplyJobStatus.SUPERSEDED,
        )
    )
    model = RepairingPriceModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert model.calls == 2
    assert worker.drafts.list()[-1].reply_text == "先量下电池仓"


def test_missing_product_safety_issue_still_hands_off() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "电池冒烟了，我要投诉")})
    repository = FakeRepository()
    repository.product = None
    model = FakeModel()
    worker = _worker(adapter, clock, repository, model)

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert worker.drafts.list()[-1].status is ReplyJobStatus.HANDOFF
    assert repository.handoffs
    assert model.calls == 0
    assert adapter.sent == []
    assert worker.status is ReceptionStatus.RUNNING


def test_handoff_isolates_only_that_conversation_and_other_customers_continue() -> None:
    clock = FakeClock()
    adapter = FakeAdapter(
        {
            "needs-human": _snapshot("needs-human", "电池冒烟了，我要投诉"),
            "normal": _snapshot("normal", "6030尺寸多大？"),
        }
    )
    repository = FakeRepository()
    model = FakeModel("6030尺寸17-18-32")
    worker = CustomerServiceWorker(
        adapter,
        repository,
        model,
        config=CustomerServiceConfig(mode=ReceptionMode.AUTO_SEND),
        clock=clock,
    )

    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert repository.handoffs == [
        ("needs-human", "未匹配商品的会话涉及安全、售后或争议问题。")
    ]
    assert adapter.sent == ["6030尺寸17-18-32\n你是二轮还是三轮？我再帮你核下适配和续航"]
    assert worker.status is ReceptionStatus.RUNNING

    handoff_open_count = adapter.opened.count("needs-human")
    adapter.snapshots["needs-human"] = _snapshot(
        "needs-human",
        "电池冒烟了，我要投诉",
        "怎么还不回复？",
    )
    worker.run_once()

    assert adapter.opened.count("needs-human") == handoff_open_count
    assert worker.status is ReceptionStatus.RUNNING


def test_resolved_handoff_does_not_reprocess_same_batch_after_restart() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "电池冒烟了，我要投诉")})
    repository = FakeRepository()

    first_worker = _worker(adapter, clock, repository, FakeModel())
    first_worker.start()
    first_worker.run_once()
    clock.advance(10)
    first_worker.run_once()
    assert repository.handoffs

    repository.handoffs.clear()
    reopened_count = len(adapter.opened)
    restarted_worker = _worker(adapter, clock, repository, FakeModel())
    restarted_worker.start()

    assert restarted_worker.run_once() == 0
    assert len(adapter.opened) == reopened_count + 1
    assert repository.handoffs == []


def test_new_message_supersedes_old_debounced_job() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "第一条")})
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()
    worker.run_once()

    adapter.snapshots["c1"] = _snapshot("c1", "第一条", "补充问题")
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    statuses = [draft.status for draft in worker.drafts.list()]
    assert ReplyJobStatus.SUPERSEDED in statuses
    assert statuses[-1] is ReplyJobStatus.AWAITING_REVIEW


def test_round_robin_scheduler_limits_each_round_and_prevents_starvation() -> None:
    clock = FakeClock()
    snapshots = {f"c{index}": _snapshot(f"c{index}", f"问题 {index}") for index in range(12)}
    adapter = FakeAdapter(snapshots)
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()

    worker.run_once()
    first_round = adapter.opened[:]
    worker.run_once()
    second_round = adapter.opened[len(first_round) :]

    assert len(first_round) == 10
    assert len(second_round) == 10
    assert set(second_round[:2]) == {"c10", "c11"}


def test_approval_rereads_and_discards_draft_when_new_message_arrives() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "原问题")})
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    draft = worker.drafts.list()[-1]

    adapter.snapshots["c1"] = _snapshot("c1", "原问题", "新问题")
    result = worker.approve_draft(draft.job_id)

    assert result.sent is False
    assert result.status is ReplyJobStatus.SUPERSEDED
    assert adapter.sent == []


def test_approval_sends_text_only_after_reread_and_confirms_outgoing() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "请问多少钱？")})
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    draft = worker.drafts.list()[-1]

    result = worker.approve_draft(draft.job_id, "您好，价格请以页面为准。")

    assert result.sent is True
    assert result.status is ReplyJobStatus.SENT
    assert adapter.sent == ["您好，价格请以页面为准。"]
    assert repository.processed
    assert any(
        getattr(item, "trust_level", None) == "human_confirmed" for item in repository.saved
    )


def test_send_not_performed_returns_draft_to_manual_review() -> None:
    clock = FakeClock()
    adapter = RetryableSendAdapter({"c1": _snapshot("c1", "请问多少钱？")})
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    draft = worker.drafts.list()[-1]

    result = worker.approve_draft(draft.job_id, "价格看页面")

    assert result.sent is False
    assert result.status is ReplyJobStatus.AWAITING_REVIEW
    assert worker.drafts.get(draft.job_id).status is ReplyJobStatus.AWAITING_REVIEW
    assert repository.processed == set()


def test_edit_draft_validates_and_persists_without_sending() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "请问多少钱？")})
    repository = FakeRepository()
    worker = _worker(adapter, clock, repository, FakeModel())
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()
    draft = worker.drafts.list()[-1]

    updated = worker.edit_draft(draft.job_id, "您好，价格请以页面为准～")

    assert updated.reply_text == "您好，价格请以页面为准～"
    assert worker.drafts.get(draft.job_id) == updated
    assert adapter.sent == []
    assert repository.saved[-1].reply_text == "您好，价格请以页面为准～"


def test_model_output_parser_rejects_prose_and_accepts_fenced_json() -> None:
    proposal = parse_reply_proposal('```json\n{"reply_text":"好的"}\n```')
    assert proposal.reply_text == "好的"

    try:
        parse_reply_proposal("好的，我来帮您处理")
    except ValueError:
        pass
    else:
        raise AssertionError("prose must never bypass the JSON parser")


def test_stop_is_interruptible_and_restart_does_not_replay_stale_stop_command() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "问题")})
    worker = _worker(adapter, clock, FakeRepository(), FakeModel())
    worker.start()

    worker.request_stop()
    assert worker.run_once() == 0
    worker.stop()
    assert worker.status.value == "stopped"
    assert worker.start().value == "running"
    assert worker.run_once() == 1


def test_auto_mode_sends_verified_text_but_does_not_create_human_style_example() -> None:
    clock = FakeClock()
    adapter = FakeAdapter({"c1": _snapshot("c1", "6030尺寸能装进车里吗？")})
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FakeModel(),
        config=CustomerServiceConfig(mode="auto_send"),
        clock=clock,
    )
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert adapter.sent == [
        "您好，测试商品目前按页面价格出售。\n你是二轮还是三轮？我再帮你核下适配和续航"
    ]
    assert worker.drafts.list()[-1].status is ReplyJobStatus.SENT
    assert not any(getattr(item, "trust_level", None) == "human_confirmed" for item in repository.saved)


def test_repeated_model_failures_halt_auto_reception() -> None:
    clock = FakeClock()
    snapshots = {
        f"c{index}": _snapshot(f"c{index}", f"6030尺寸能装进第{index}辆车吗？")
        for index in range(3)
    }
    adapter = FakeAdapter(snapshots)
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FailingModel(),
        config=CustomerServiceConfig(
            mode=ReceptionMode.AUTO_SEND,
            max_model_attempts=1,
            max_model_failures=3,
        ),
        clock=clock,
    )
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert worker.status.value == "halted"
    assert adapter.sent == []


def test_repeated_send_failures_halt_auto_reception() -> None:
    clock = FakeClock()
    snapshots = {
        f"c{index}": _snapshot(f"c{index}", f"6030尺寸能装进第{index}辆车吗？")
        for index in range(3)
    }
    adapter = FailingSendAdapter(snapshots)
    repository = FakeRepository()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        FakeModel(),
        config=CustomerServiceConfig(
            mode=ReceptionMode.AUTO_SEND,
            max_send_failures=3,
        ),
        clock=clock,
    )
    worker.start()
    worker.run_once()
    clock.advance(10)
    worker.run_once()

    assert worker.status is ReceptionStatus.HALTED
    assert len(
        [draft for draft in worker.drafts.list() if draft.status is ReplyJobStatus.FAILED]
    ) == 3
    assert repository.processed == set()

"""Run bounded customer-service dialogue simulations without opening Xianyu."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xianyu_assistant.customer_service.customer_service_worker import CustomerServiceWorker
from xianyu_assistant.customer_service.deepseek_client import DeepSeekClient
from xianyu_assistant.customer_service.fingerprints import content_fingerprint
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    CustomerServiceConfig,
    DeepSeekSettings,
    HistoricalExample,
    MessageDirection,
    MessageKind,
    PageHealth,
    PageHealthStatus,
    PriceChangeDraft,
    PriceChangeReceipt,
    ReceptionMode,
    ReplyDraft,
    ReplyJobStatus,
    SendReceipt,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import KeyringCredentialStore


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    turns: tuple[tuple[MessageDirection, str], ...]
    expected_status: ReplyJobStatus = ReplyJobStatus.SENT
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    check_seller_style: bool = True
    expected_price_change: str | None = None
    expected_negotiation_outcome: str | None = None


class SimulationClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 9, 12, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


class SimulationAdapter:
    """Keep all simulated sends in memory; never connect to a browser."""

    def __init__(self, scenario: Scenario) -> None:
        self.conversation_key = f"simulation-{scenario.name}"
        self.messages = [
            ChatMessage(
                f"{self.conversation_key}-{index}",
                direction,
                MessageKind.TEXT,
                text,
            )
            for index, (direction, text) in enumerate(scenario.turns, 1)
        ]
        self.sent: list[str] = []
        self.changed_prices: list[str] = []

    def open_dedicated_chat_page(self) -> None:
        pass

    def check_page_health(self) -> PageHealth:
        return PageHealth(PageHealthStatus.HEALTHY)

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        del limit
        return [ConversationSummary(self.conversation_key)]

    def open_conversation(self, conversation_key: str) -> None:
        if conversation_key != self.conversation_key:
            raise RuntimeError("simulation conversation mismatch")

    def read_conversation(self) -> ConversationSnapshot:
        return ConversationSnapshot(self.conversation_key, tuple(self.messages))

    def request_voice_transcript(self, message_key: str) -> str | None:
        del message_key
        return None

    def capture_incoming_image(self, message_key: str):
        del message_key
        raise RuntimeError("image simulation is not enabled")

    def send_text(self, text: str) -> SendReceipt:
        self.sent.append(text)
        message_key = f"{self.conversation_key}-sent-{len(self.sent)}"
        self.messages.append(
            ChatMessage(message_key, MessageDirection.OUTGOING, MessageKind.TEXT, text)
        )
        return SendReceipt(message_key, content_fingerprint(text))

    def send_approved_image(self, path: Path) -> SendReceipt:
        del path
        raise RuntimeError("simulation never sends images")

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        return bool(
            self.sent
            and content_fingerprint(self.sent[-1]) == receipt.content_fingerprint
        )

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        """Record a synthetic change only; this adapter never opens Xianyu."""
        self.changed_prices.append(approved_price)
        return PriceChangeReceipt(previous_price="5.00", applied_price=approved_price)


class SimulationRepository:
    """Delegate knowledge reads to the real DB and keep every write in memory."""

    def __init__(self, source: CustomerServiceRepository, *, has_prior_turn: bool) -> None:
        self.source = source
        self.has_prior_turn = has_prior_turn
        self.drafts: dict[str, ReplyDraft] = {}
        self.processed: set[str] = set()
        self.handoffs: list[str] = []
        self.price_changes: list[PriceChangeDraft] = []

    def find_product_knowledge(self, **kwargs):
        return self.source.find_product_knowledge(**kwargs)

    def find_product_knowledge_for_query(self, query: str):
        return self.source.find_product_knowledge_for_query(query)

    def get_product_knowledge(self, product_key: str):
        return self.source.get_product_knowledge(product_key)

    def find_historical_examples(self, query: str, limit: int):
        return self.source.find_historical_examples(query, limit)

    def find_curated_knowledge_answers(self, query: str, limit: int):
        return self.source.find_curated_knowledge_answers(query, limit)

    def find_knowledge_answers(self, query: str, limit: int):
        return self.source.find_knowledge_answers(query, limit)

    def has_prior_reply_job(self, conversation_key: str, *, exclude_job_id: str) -> bool:
        return self.has_prior_turn or any(
            draft.conversation_key == conversation_key and draft.job_id != exclude_job_id
            for draft in self.drafts.values()
        )

    def get_media_asset(self, asset_id: str):
        return self.source.get_media_asset(asset_id)

    def save_price_change_draft(self, draft: PriceChangeDraft) -> None:
        self.price_changes = [
            item for item in self.price_changes if item.task_id != draft.task_id
        ]
        self.price_changes.append(draft)

    def save_reply_draft(self, draft: ReplyDraft) -> None:
        self.drafts[draft.job_id] = draft

    def find_reply_draft(self, *, conversation_key: str, batch_fingerprint: str):
        return next(
            (
                draft
                for draft in self.drafts.values()
                if draft.conversation_key == conversation_key
                and draft.batch_fingerprint == batch_fingerprint
            ),
            None,
        )

    def has_processed_fingerprint(self, batch_fingerprint: str) -> bool:
        return batch_fingerprint in self.processed

    def add_processed_fingerprint(self, batch_fingerprint: str, observed_at: datetime) -> None:
        del observed_at
        self.processed.add(batch_fingerprint)

    def save_human_confirmed_example(self, example: HistoricalExample) -> None:
        del example

    def save_handoff_event(
        self,
        *,
        conversation_key: str,
        reason: str,
        created_at: datetime,
    ) -> None:
        del conversation_key, created_at
        self.handoffs.append(reason)

    def has_open_handoff(self, conversation_key: str) -> bool:
        del conversation_key
        return bool(self.handoffs)


SCENARIOS = (
    Scenario(
        "first_vague_inquiry",
        ((MessageDirection.INCOMING, "怎么卖？"),),
        required_terms=("原装25年铁塔", "60V20A", "398元"),
        check_seller_style=False,
    ),
    Scenario(
        "price_then_model_answer",
        (
            (MessageDirection.INCOMING, "多少钱？"),
            (MessageDirection.OUTGOING, "你要哪个型号"),
            (MessageDirection.INCOMING, "6020"),
        ),
        required_terms=("398",),
        forbidden_terms=("哪个型号", "尺寸", "容量", "健康度", "续航"),
    ),
    Scenario(
        "model_then_short_price_followup",
        (
            (MessageDirection.INCOMING, "6020"),
            (MessageDirection.OUTGOING, "6020尺寸14.5-17-29"),
            (MessageDirection.INCOMING, "多少钱？"),
        ),
        required_terms=("398",),
        forbidden_terms=("哪个型号", "最低", "378"),
    ),
    Scenario(
        "switch_to_6030",
        (
            (MessageDirection.INCOMING, "6020多少钱？"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "那6030呢，多少钱？"),
        ),
        required_terms=("678",),
        forbidden_terms=("398",),
    ),
    Scenario(
        "shipping_after_model",
        (
            (MessageDirection.INCOMING, "6030"),
            (MessageDirection.OUTGOING, "6030 678元"),
            (MessageDirection.INCOMING, "这个包邮吗？"),
        ),
        required_terms=("包邮",),
        forbidden_terms=("哪个型号",),
    ),
    Scenario(
        "size_after_model",
        (
            (MessageDirection.INCOMING, "6020多少钱？"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "尺寸多大？"),
        ),
        required_terms=("14.5", "17", "29"),
    ),
    Scenario(
        "health_after_model",
        (
            (MessageDirection.INCOMING, "60V30Ah多少钱？"),
            (MessageDirection.OUTGOING, "60V30Ah 678元"),
            (MessageDirection.INCOMING, "健康度呢？"),
        ),
        required_terms=("97",),
    ),
    Scenario(
        "bluetooth_price_followup",
        (
            (MessageDirection.INCOMING, "6020多少钱？"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "加蓝牙多少钱？"),
        ),
        required_terms=("20",),
    ),
    Scenario(
        "correct_previous_model",
        (
            (MessageDirection.INCOMING, "6020多少钱？"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "不是6020，换6030，多少钱？"),
        ),
        required_terms=("678",),
        forbidden_terms=("398",),
    ),
    Scenario(
        "ask_two_model_prices",
        (
            (MessageDirection.INCOMING, "怎么卖？"),
            (MessageDirection.OUTGOING, "你要哪个型号"),
            (MessageDirection.INCOMING, "6020和6030分别多少钱？"),
        ),
        required_terms=("398", "678"),
    ),
    Scenario(
        "split_voltage_and_capacity",
        (
            (MessageDirection.INCOMING, "60V的多少钱？"),
            (MessageDirection.OUTGOING, "要多大容量"),
            (MessageDirection.INCOMING, "30Ah"),
        ),
        required_terms=("678",),
        forbidden_terms=("哪个型号",),
    ),
    Scenario(
        "unsafe_battery_handoff",
        ((MessageDirection.INCOMING, "电池冒烟了，我要退货"),),
        expected_status=ReplyJobStatus.HANDOFF,
        check_seller_style=False,
    ),
    Scenario(
        "unsupported_customer_price",
        (
            (MessageDirection.INCOMING, "6020多少钱？"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "200卖不卖？"),
        ),
        required_terms=("不行",),
    ),
)

NEGOTIATION_SCENARIOS = (
    Scenario(
        "nego_6020_small_discount",
        (
            (MessageDirection.INCOMING, "6020多少钱"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "380能卖吗"),
        ),
        required_terms=("380",),
        expected_negotiation_outcome="accept",
    ),
    Scenario(
        "nego_6020_below_floor",
        (
            (MessageDirection.INCOMING, "6020多少钱"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "350行不行"),
        ),
        required_terms=("不行",),
        expected_negotiation_outcome="reject",
    ),
    Scenario(
        "nego_6030_at_floor",
        (
            (MessageDirection.INCOMING, "6030多少钱"),
            (MessageDirection.OUTGOING, "6030 678元"),
            (MessageDirection.INCOMING, "650可以吗"),
        ),
        required_terms=("650",),
        expected_negotiation_outcome="accept",
    ),
    Scenario(
        "nego_6030_below_floor",
        (
            (MessageDirection.INCOMING, "6030多少钱"),
            (MessageDirection.OUTGOING, "6030 678元"),
            (MessageDirection.INCOMING, "620卖不卖"),
        ),
        required_terms=("不行",),
        expected_negotiation_outcome="reject",
    ),
    Scenario(
        "nego_4830_middle_offer",
        (
            (MessageDirection.INCOMING, "4830多少钱"),
            (MessageDirection.OUTGOING, "4830 518元"),
            (MessageDirection.INCOMING, "500能出吗"),
        ),
        required_terms=("500",),
        expected_negotiation_outcome="accept",
    ),
    Scenario(
        "nego_4830_below_floor_with_shipping",
        (
            (MessageDirection.INCOMING, "4830多少钱"),
            (MessageDirection.OUTGOING, "4830 518元"),
            (MessageDirection.INCOMING, "450包邮行不行"),
        ),
        required_terms=("不行",),
        expected_negotiation_outcome="reject",
    ),
    Scenario(
        "nego_6020_two_units_total",
        (
            (MessageDirection.INCOMING, "6020多少钱"),
            (MessageDirection.OUTGOING, "6020 398元"),
            (MessageDirection.INCOMING, "两组750可以吗"),
        ),
        expected_negotiation_outcome="reject",
    ),
    Scenario(
        "nego_6020_repeated_pressure",
        (
            (MessageDirection.INCOMING, "6020 350行不行"),
            (MessageDirection.OUTGOING, "不行 398"),
            (MessageDirection.INCOMING, "那370呢"),
        ),
        required_terms=("不行",),
        expected_negotiation_outcome="reject",
    ),
    Scenario(
        "nego_switch_to_4830",
        (
            (MessageDirection.INCOMING, "6030 650可以吗"),
            (MessageDirection.OUTGOING, "可以"),
            (MessageDirection.INCOMING, "换4830 480可以不"),
        ),
        required_terms=("480",),
        expected_negotiation_outcome="accept",
    ),
    Scenario(
        "nego_order_ready_for_price_change",
        (
            (MessageDirection.INCOMING, "6030 650可以吗"),
            (MessageDirection.OUTGOING, "可以"),
            (MessageDirection.INCOMING, "我拍下了 待付款 650改价"),
        ),
        required_terms=("650",),
        expected_price_change="650.00",
        expected_negotiation_outcome="accept",
    ),
)


def run_scenario(
    scenario: Scenario,
    source_repository: CustomerServiceRepository,
    model: DeepSeekClient,
    settings: DeepSeekSettings,
) -> dict[str, object]:
    adapter = SimulationAdapter(scenario)
    repository = SimulationRepository(
        source_repository,
        has_prior_turn=any(
            direction is MessageDirection.OUTGOING for direction, _text in scenario.turns
        ),
    )
    clock = SimulationClock()
    worker = CustomerServiceWorker(
        adapter,
        repository,
        model,
        config=CustomerServiceConfig(
            mode=ReceptionMode.AUTO_SEND,
            debounce_seconds=1,
            poll_interval_seconds=1,
        ),
        clock=clock,
        text_model=settings.text_model,
        vision_model=settings.vision_model,
    )
    worker.start()
    worker.run_once()
    clock.advance(1)
    worker.run_once()
    final_draft = list(repository.drafts.values())[-1]
    reply = adapter.sent[-1] if adapter.sent else ""
    failures: list[str] = []
    if final_draft.status is not scenario.expected_status:
        failures.append(
            f"状态应为 {scenario.expected_status.value}，实际为 {final_draft.status.value}"
        )
    for term in scenario.required_terms:
        if term not in reply:
            failures.append(f"回复缺少：{term}")
    for term in scenario.forbidden_terms:
        if term in reply:
            failures.append(f"回复不应包含：{term}")
    style_failures: list[str] = []
    if scenario.check_seller_style and reply:
        for phrase in ("您好", "亲", "感谢咨询", "很高兴为您服务", "请问还有什么"):
            if phrase in reply:
                style_failures.append(f"出现客服腔：{phrase}")
        if "!" in reply or "！" in reply or "~" in reply or "～" in reply:
            style_failures.append("出现卖家历史语料未使用的感叹或波浪号")
        if len("".join(reply.split())) > 80:
            style_failures.append("普通回复超过80个非空白字符")
        if scenario.name.startswith("nego_") and "最低" in reply:
            style_failures.append("主动使用“最低”暴露议价边界")
    failures.extend(style_failures)
    if scenario.expected_negotiation_outcome is not None and reply:
        rejection_markers = ("不行", "出不了", "做不到", "不能", "不卖")
        rejected = any(marker in reply for marker in rejection_markers)
        if scenario.expected_negotiation_outcome == "accept" and rejected:
            failures.append("报价在可接受区间内，但回复错误拒绝")
        if scenario.expected_negotiation_outcome == "reject" and not rejected:
            failures.append("报价低于可接受区间，但回复没有明确拒绝")
    queued_price_change = repository.price_changes[-1] if repository.price_changes else None
    if scenario.expected_price_change is not None:
        actual_price = None if queued_price_change is None else queued_price_change.proposed_price
        if actual_price != scenario.expected_price_change:
            failures.append(
                f"应生成改价建议 {scenario.expected_price_change}，实际为 {actual_price or '无'}"
            )
    conversation = [
        {
            "speaker": "顾客" if direction is MessageDirection.INCOMING else "商家（既有上下文）",
            "text": text,
        }
        for direction, text in scenario.turns
    ]
    if reply:
        conversation.append({"speaker": "程序生成的商家回复", "text": reply})
    return {
        "scenario": scenario.name,
        "conversation": conversation,
        "customer_message": scenario.turns[-1][1],
        "status": final_draft.status.value,
        "reply": reply,
        "handoff_reason": repository.handoffs[-1] if repository.handoffs else None,
        "seller_style_checked": scenario.check_seller_style,
        "seller_style_failures": style_failures,
        "queued_price_change": (
            None
            if queued_price_change is None
            else {
                "product": queued_price_change.product_name,
                "customer_offer": queued_price_change.customer_offer,
                "proposed_price": queued_price_change.proposed_price,
                "minimum_price": queued_price_change.minimum_price,
                "listed_price": queued_price_change.listed_price,
                "status": queued_price_change.status.value,
            }
        ),
        "synthetic_applied_prices": list(adapter.changed_prices),
        "expected_negotiation_outcome": scenario.expected_negotiation_outcome,
        "passed": not failures,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/客服对话模拟测试报告.json"),
    )
    parser.add_argument(
        "--suite",
        choices=("core", "negotiation"),
        default="core",
        help="选择基础问答或议价专项场景。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只运行前 N 个独立虚拟顾客场景。",
    )
    args = parser.parse_args()
    suite_scenarios = NEGOTIATION_SCENARIOS if args.suite == "negotiation" else SCENARIOS
    if args.limit is not None and not 1 <= args.limit <= len(suite_scenarios):
        parser.error(f"--limit 必须在 1 到 {len(suite_scenarios)} 之间")
    database_path = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.cwd())))
        / "XianyuAssistant"
        / "xianyu_assistant.db"
    )
    repository = CustomerServiceRepository(database_path)
    settings = DeepSeekSettings(
        base_url=repository.get_setting("deepseek_base_url", "https://api.deepseek.com")
        or "https://api.deepseek.com",
        text_model=repository.get_setting("deepseek_text_model", "deepseek-chat")
        or "deepseek-chat",
        vision_model=repository.get_setting("deepseek_vision_model", "deepseek-chat")
        or "deepseek-chat",
    )
    model = DeepSeekClient(settings, KeyringCredentialStore())
    selected_scenarios = (
        suite_scenarios[: args.limit] if args.limit is not None else suite_scenarios
    )
    results = [
        run_scenario(scenario, repository, model, settings)
        for scenario in selected_scenarios
    ]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "safety": "Synthetic conversations only; no browser or Xianyu send adapter was used.",
        "model": settings.text_model,
        "suite": args.suite,
        "simulated_user_count": len(selected_scenarios),
        "passed": sum(bool(item["passed"]) for item in results),
        "failed": sum(not bool(item["passed"]) for item in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

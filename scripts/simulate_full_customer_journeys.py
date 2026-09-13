"""Run ten complete multi-turn customer journeys without opening Xianyu."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from simulate_customer_service_dialogues import (
    Scenario,
    SimulationAdapter,
    SimulationClock,
    SimulationRepository,
)

from xianyu_assistant.customer_service.customer_service_worker import CustomerServiceWorker
from xianyu_assistant.customer_service.deepseek_client import DeepSeekClient
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    CustomerServiceConfig,
    DeepSeekSettings,
    MessageDirection,
    MessageKind,
    ReceptionMode,
    ReplyJobStatus,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import KeyringCredentialStore


@dataclass(frozen=True, slots=True)
class Journey:
    name: str
    customer_turns: tuple[str, ...]


JOURNEYS = (
    Journey(
        "6020普通购买",
        (
            "怎么卖",
            "我想要6020",
            "尺寸多大",
            "健康度呢",
            "那就要这个 怎么下单",
        ),
    ),
    Journey(
        "6030议价并拍下",
        (
            "6030多少钱",
            "650可以吗",
            "可以的话我就拍",
            "我拍下了 待付款 给我改650",
        ),
    ),
    Journey(
        "4830续航咨询",
        (
            "48伏30安有吗",
            "多少钱",
            "尺寸多大",
            "二轮一个人能跑多远",
        ),
    ),
    Journey(
        "两款价格比较",
        (
            "6020和6030有什么区别",
            "分别多少钱",
            "我平时跑50公里选哪个",
            "那6030能少点吗",
        ),
    ),
    Journey(
        "蓝牙配置咨询",
        (
            "6020带蓝牙吗",
            "加装多少钱",
            "用什么软件连接",
            "原装蓝牙能用吗",
        ),
    ),
    Journey(
        "包邮与发货咨询",
        (
            "6030包邮吗",
            "从哪里发",
            "今天能发吗",
            "大概多久能到",
        ),
    ),
    Journey(
        "到货充电与安装",
        (
            "6020刚收到要先充电吗",
            "大概要充多久",
            "充满了再装车对吧",
            "接口不会接怎么办",
        ),
    ),
    Journey(
        "安全问题转人工",
        (
            "电池刚才冒烟了",
            "现在要怎么处理",
        ),
    ),
    Journey(
        "6020连续压价",
        (
            "6020多少钱",
            "380能卖吗",
            "那378行不行",
            "可以我就拍",
        ),
    ),
    Journey(
        "两组6020批量购买",
        (
            "6020要两组",
            "两组多少钱",
            "750可以吗",
            "最低能做到多少",
        ),
    ),
)


def _settings(repository: CustomerServiceRepository) -> DeepSeekSettings:
    return DeepSeekSettings(
        base_url=repository.get_setting("deepseek_base_url", "https://api.deepseek.com")
        or "https://api.deepseek.com",
        text_model=repository.get_setting("deepseek_text_model", "deepseek-chat")
        or "deepseek-chat",
        vision_model=repository.get_setting("deepseek_vision_model", "deepseek-chat")
        or "deepseek-chat",
    )


def run_journey(
    journey: Journey,
    source_repository: CustomerServiceRepository,
    model: DeepSeekClient,
    settings: DeepSeekSettings,
) -> dict[str, object]:
    scenario = Scenario(journey.name, ())
    adapter = SimulationAdapter(scenario)
    repository = SimulationRepository(source_repository, has_prior_turn=False)
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
    transcript: list[dict[str, str]] = []
    failures: list[str] = []

    for index, customer_text in enumerate(journey.customer_turns, 1):
        message_key = f"{adapter.conversation_key}-customer-{index}"
        adapter.messages.append(
            ChatMessage(
                message_key,
                MessageDirection.INCOMING,
                MessageKind.TEXT,
                customer_text,
            )
        )
        transcript.append({"speaker": "顾客", "text": customer_text})
        sent_before = len(adapter.sent)
        worker.run_once()
        clock.advance(1)
        worker.run_once()

        if len(adapter.sent) > sent_before:
            reply = adapter.sent[-1]
            transcript.append({"speaker": "商家", "text": reply})
            if any(
                phrase in reply
                for phrase in ("您好", "亲", "感谢咨询", "很高兴为您服务")
            ):
                failures.append(f"第{index}轮出现客服腔")
        elif repository.handoffs:
            transcript.append({"speaker": "系统", "text": "已转人工并暂停该顾客自动回复"})
            break
        else:
            latest_draft = list(repository.drafts.values())[-1]
            failures.append(f"第{index}轮未回复，状态为{latest_draft.status.value}")
            if latest_draft.status is ReplyJobStatus.FAILED:
                break

    queued_price_changes = [
        {
            "product": task.product_name,
            "customer_offer": task.customer_offer,
            "proposed_price": task.proposed_price,
            "minimum_price": task.minimum_price,
            "listed_price": task.listed_price,
            "status": task.status.value,
        }
        for task in repository.price_changes
    ]
    return {
        "journey": journey.name,
        "transcript": transcript,
        "handoff": bool(repository.handoffs),
        "handoff_reason": repository.handoffs[-1] if repository.handoffs else None,
        "queued_price_changes": queued_price_changes,
        "passed_transport_and_state": not failures,
        "automatic_failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/客服完整多轮对话模拟_10用户.json"),
    )
    args = parser.parse_args()
    database_path = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.cwd())))
        / "XianyuAssistant"
        / "xianyu_assistant.db"
    )
    repository = CustomerServiceRepository(database_path)
    settings = _settings(repository)
    model = DeepSeekClient(settings, KeyringCredentialStore())
    results = [run_journey(journey, repository, model, settings) for journey in JOURNEYS]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "safety": "Synthetic multi-turn conversations only; no browser or Xianyu adapter was used.",
        "model": settings.text_model,
        "simulated_user_count": len(results),
        "transport_and_state_passed": sum(
            bool(result["passed_transport_and_state"]) for result in results
        ),
        "transport_and_state_failed": sum(
            not bool(result["passed_transport_and_state"]) for result in results
        ),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["transport_and_state_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

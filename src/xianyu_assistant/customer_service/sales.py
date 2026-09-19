"""Deterministic, moderate sales guidance layered on top of factual replies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    MessageDirection,
    ProductKnowledge,
    ReplyProposal,
    SalesStage,
)

_NO_SALES_MARKERS = (
    "退款",
    "退货",
    "售后",
    "投诉",
    "纠纷",
    "赔偿",
    "坏了",
    "故障",
    "不能用",
    "用不了",
    "不工作",
    "断电",
    "起火",
    "冒烟",
    "爆炸",
    "鼓包",
    "物流不动",
    "没收到",
    "未收到",
    "少发",
    "漏发",
    "质保",
    "保修",
    "虚标",
    "容量不足",
)
_TRANSACTION_MARKERS = (
    "已拍",
    "拍下了",
    "付款了",
    "已付款",
    "改价",
    "订单",
    "发货",
    "单号",
)
_VEHICLE_MARKERS = ("二轮", "三轮", "车型", "电动车", "电机")
_RANGE_MARKERS = ("续航", "跑多远", "公里", "通勤", "长途")
_QUESTION_RE = re.compile(r"[?？]")


@dataclass(frozen=True, slots=True)
class SalesPlan:
    """One reply enhancement plus an optional, persisted single follow-up."""

    proposal: ReplyProposal
    stage: SalesStage
    follow_up_text: str | None = None


def build_sales_plan(
    query: str,
    proposal: ReplyProposal,
    knowledge: ProductKnowledge | None,
    messages: Sequence[ChatMessage],
    *,
    negotiation_active: bool,
    recent_merchant_replies: Sequence[str] = (),
) -> SalesPlan:
    """Add at most one relevant sales move without changing factual decisions."""

    folded = normalize_for_matching(query)
    if (
        proposal.requires_handoff
        or proposal.intent == "handoff"
        or proposal.intent == "warranty"
        or negotiation_active
        or any(marker in folded for marker in _NO_SALES_MARKERS)
        or any(marker in folded for marker in _TRANSACTION_MARKERS)
    ):
        return SalesPlan(proposal, SalesStage.PAUSED)

    customer_text = " ".join(
        (message.text or "")
        for message in messages
        if message.direction is MessageDirection.INCOMING
    )
    customer_folded = normalize_for_matching(customer_text)

    if proposal.intent == "battery_catalog":
        return SalesPlan(
            proposal,
            SalesStage.RECOMMEND,
            "你是二轮还是三轮，平时想跑多少公里？我按用途给你配",
        )

    if knowledge is None:
        nudge = "你是二轮还是三轮？平时想跑多少公里"
        enhanced = _append_once(proposal, nudge, recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            "方便说下车型和想跑的公里数，我给你配合适的",
        )

    has_vehicle = any(marker in customer_folded for marker in _VEHICLE_MARKERS)
    has_range = any(marker in customer_folded for marker in _RANGE_MARKERS)
    if not has_vehicle and not _QUESTION_RE.search(proposal.reply_text):
        nudge = "你是二轮还是三轮？我再帮你核下适配和续航"
        enhanced = _append_once(proposal, nudge, recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            "方便说下车型和想跑的公里数，我再帮你核下适配",
        )
    if not has_range and not _QUESTION_RE.search(proposal.reply_text):
        nudge = "平时想跑多少公里？我按用途帮你看"
        enhanced = _append_once(proposal, nudge, recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.RECOMMEND,
            "你平时主要通勤还是跑长途？我按续航帮你看",
        )

    nudge = "默认送充电器，需要蓝牙可以加20元"
    enhanced = _append_once(proposal, nudge, recent_merchant_replies)
    return SalesPlan(
        enhanced,
        SalesStage.PROVE_VALUE,
        "这款默认送充电器，需要的话我再帮你确认下单",
    )


def _append_once(
    proposal: ReplyProposal,
    nudge: str,
    recent_merchant_replies: Sequence[str],
) -> ReplyProposal:
    normalized_nudge = normalize_for_matching(nudge)
    if normalized_nudge in normalize_for_matching(proposal.reply_text):
        return proposal
    if any(normalized_nudge in normalize_for_matching(reply) for reply in recent_merchant_replies):
        return proposal
    return replace(proposal, reply_text=f"{proposal.reply_text.rstrip()}\n{nudge}")

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
    "已下单",
    "下单了",
    "已经下单",
    "付款了",
    "已付款",
    "已支付",
    "支付了",
    "改价",
    "订单",
    "发货",
    "单号",
)
_TRANSACTION_REPLY_MARKERS = (
    "准备给你发货",
    "准备发货",
    "给你发货",
    "已经发货",
    "已发货",
    "订单已",
    "物流单号",
)
_ROUTINE_SERVICE_MARKERS = (
    "充电器",
    "匹配",
    "接口",
    "插头",
    "怎么装",
    "安装",
    "怎么用",
    "充电",
    "快递",
    "物流",
    "包邮",
    "运费",
    "哪里发",
    "发哪里",
)
_SALES_QUESTION_MARKERS = (
    "多少钱",
    "多钱",
    "什么价",
    "价格",
    "怎么卖",
    "推荐",
    "怎么选",
    "选哪个",
    "选哪款",
    "哪款合适",
    "买哪个",
    "买哪款",
    "配哪个",
    "配哪款",
    "要多大",
    "多大容量",
)
_ROUTINE_INTENTS = frozenset(
    {
        "charger",
        "charging_duration",
        "default_carriers",
        "first_use",
        "order_status",
        "pickup",
        "shipping",
        "shipping_origin",
        "shipping_restricted",
    }
)
_VEHICLE_MARKERS = ("二轮", "三轮", "车型", "电动车", "电机")
_RANGE_MARKERS = ("续航", "跑多远", "公里", "通勤", "长途")
_QUESTION_RE = re.compile(r"[?？]")
_BUYING_MARKERS = (
    "想买", "我要买", "我要一组", "我要一套", "我要这款",
    "要一组", "要一套", "能装就要", "确定要", "打算买",
    "就要这款", "这款要了", "就这个", "就这款", "可以我就拍", "行的话就拍",
    "我拍", "我下单", "准备拍", "准备下单", "现在拍", "现在下单",
)
_DECLINE_MARKERS = (
    "不买", "不要了", "不要一组", "不要一套", "先不", "再看看", "考虑一下", "算了",
)
_HOW_TO_BUY_MARKERS = (
    "怎么拍", "怎么买", "怎么下单", "在哪里拍", "如何下单", "怎么操作", "哪里点立即购买",
)
_FIT_CONDITION_MARKERS = ("能装就要", "能用就要", "合适就要", "合适就买")
_CHECKOUT_GUIDANCE_VARIANTS = (
    "点右上角“立即购买”先提交订单，支付密码先别输，取消这次付款，保留待付款订单。我改好价格通知你后再付款。",
    "从右上角“立即购买”拍下就行，先不要输入支付密码，退出付款页面让订单保持待付款。等我改好价告诉你，再付款。",
    "右上角“立即购买”先生成订单，付款这一步先取消，不用输支付密码，也别取消订单。价格我改好后通知你再付。",
)
_IMMEDIATE_PAYMENT_MARKERS = (
    "直接付款", "马上付款", "立即付款", "现在付款", "直接支付", "立即支付",
    "现在支付", "拍下付款", "拍下就付", "输入支付密码", "输入付款密码",
)


def checkout_guidance_variant_in_text(text: str) -> int | None:
    """Recognize only a complete application-authored payment sequence."""
    folded = normalize_for_matching(text)
    for index, variant in enumerate(_CHECKOUT_GUIDANCE_VARIANTS):
        if normalize_for_matching(variant) in folded:
            return index
    return None


def remove_checkout_guidance(text: str) -> str:
    """Remove pre-order checkout components if an order appeared before sending."""
    for variant in _CHECKOUT_GUIDANCE_VARIANTS:
        text = text.replace(variant, "")
    return "\n".join(line for line in text.splitlines()
                     if not ("立即购买" in line and any(marker in line for marker in ("拍下", "订单", "付款")))).strip()


def _next_checkout_guidance_variant(
    previous_variant: int | None, recent_merchant_replies: Sequence[str]
) -> int:
    blocked = {previous_variant} if previous_variant is not None else set()
    for reply in reversed(recent_merchant_replies):
        recent_variant = checkout_guidance_variant_in_text(reply)
        if recent_variant is not None:
            blocked.add(recent_variant)
            break
    start = (previous_variant + 1) % len(_CHECKOUT_GUIDANCE_VARIANTS) if previous_variant is not None else 0
    for offset in range(len(_CHECKOUT_GUIDANCE_VARIANTS)):
        candidate = (start + offset) % len(_CHECKOUT_GUIDANCE_VARIANTS)
        if candidate not in blocked:
            return candidate
    return start


@dataclass(frozen=True, slots=True)
class SalesPlan:
    """One reply enhancement plus an optional, persisted single follow-up."""

    proposal: ReplyProposal
    stage: SalesStage
    follow_up_text: str | None = None


def build_checkout_guidance_plan(
    query: str,
    proposal: ReplyProposal,
    knowledge: ProductKnowledge | None,
    messages: Sequence[ChatMessage],
    *,
    negotiation_active: bool,
    selected_model: str | None,
    quantity: int,
    recent_merchant_replies: Sequence[str] = (),
    accepted_negotiation: bool = False,
    accepted_price: str | None = None,
    previous_guidance_variant: int | None = None,
) -> SalesPlan:
    """Optional restrained checkout guidance; the existing plan remains the default."""
    del messages
    folded = normalize_for_matching(query)
    reply = normalize_for_matching(proposal.reply_text)
    if (
        proposal.requires_handoff
        or proposal.intent in {"handoff", "warranty", "battery_catalog"}
        or proposal.intent in _ROUTINE_INTENTS
        or (negotiation_active and not accepted_negotiation)
        or any(marker in folded for marker in _NO_SALES_MARKERS)
        or any(marker in folded for marker in _DECLINE_MARKERS)
        or any(marker in folded for marker in _TRANSACTION_MARKERS)
        or any(marker in reply for marker in _TRANSACTION_REPLY_MARKERS)
    ):
        return SalesPlan(proposal, SalesStage.PAUSED)

    wants_to_buy = any(marker in folded for marker in _BUYING_MARKERS)
    asks_how = any(marker in folded for marker in _HOW_TO_BUY_MARKERS)
    if not (wants_to_buy or asks_how or accepted_negotiation):
        return SalesPlan(proposal, SalesStage.PAUSED)

    if (
        not accepted_negotiation
        and not asks_how
        and any(checkout_guidance_variant_in_text(item) is not None for item in recent_merchant_replies)
    ):
        return SalesPlan(proposal, SalesStage.PAUSED)

    if selected_model is None and not accepted_negotiation:
        if _QUESTION_RE.search(proposal.reply_text) or "哪个型号" in reply:
            follow_up = (
                "型号发我下 我帮你确认" if "哪个型号" in reply else None
            )
            return SalesPlan(proposal, SalesStage.QUALIFY, follow_up)
        enhanced = _append_once(proposal, "你要哪个型号", recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            "型号发我下 我帮你确认" if enhanced != proposal else None,
        )

    if not accepted_negotiation and any(marker in folded for marker in _FIT_CONDITION_MARKERS):
        if _QUESTION_RE.search(proposal.reply_text):
            return SalesPlan(proposal, SalesStage.QUALIFY)
        if "电池仓尺寸" in reply:
            return SalesPlan(proposal, SalesStage.QUALIFY)
        enhanced = _append_once(
            proposal, "电池仓尺寸发我下 我帮你核", recent_merchant_replies
        )
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            "电池仓尺寸方便发我下吗" if enhanced != proposal else None,
        )

    if (
        knowledge is None
        or not knowledge.listed_price
        or quantity != 1
        or (not accepted_negotiation and not _knowledge_matches_model(knowledge, selected_model))
        or _QUESTION_RE.search(proposal.reply_text)
    ):
        return SalesPlan(proposal, SalesStage.PAUSED)

    variant = _next_checkout_guidance_variant(previous_guidance_variant, recent_merchant_replies)
    current_variant = checkout_guidance_variant_in_text(proposal.reply_text)
    if current_variant is not None:
        if current_variant == variant:
            return SalesPlan(proposal, SalesStage.CLOSE)
        base_reply = proposal.reply_text.replace(
            _CHECKOUT_GUIDANCE_VARIANTS[current_variant], ""
        ).strip() or _checkout_fallback(accepted_price)
    elif any(marker in reply for marker in _IMMEDIATE_PAYMENT_MARKERS):
        base_reply = _checkout_fallback(accepted_price)
    else:
        base_reply = proposal.reply_text.rstrip()
    enhanced = replace(
        proposal,
        reply_text=f"{base_reply}\n{_CHECKOUT_GUIDANCE_VARIANTS[variant]}",
    )
    return SalesPlan(enhanced, SalesStage.CLOSE)


def _checkout_fallback(accepted_price: str | None) -> str:
    return f"可以，按{accepted_price}元" if accepted_price is not None else "好的"


def _knowledge_matches_model(knowledge: ProductKnowledge, model: str) -> bool:
    candidates = (knowledge.name, knowledge.specifications, *knowledge.aliases)
    normalized_model = normalize_for_matching(model)
    return any(normalized_model in normalize_for_matching(item) for item in candidates)


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
    reply_folded = normalize_for_matching(proposal.reply_text)
    if (
        proposal.requires_handoff
        or proposal.intent == "handoff"
        or proposal.intent == "warranty"
        or proposal.intent in _ROUTINE_INTENTS
        or negotiation_active
        or any(marker in folded for marker in _NO_SALES_MARKERS)
        or any(marker in folded for marker in _TRANSACTION_MARKERS)
        or any(marker in reply_folded for marker in _TRANSACTION_REPLY_MARKERS)
        or any(marker in folded for marker in _ROUTINE_SERVICE_MARKERS)
    ):
        return SalesPlan(proposal, SalesStage.PAUSED)

    customer_text = " ".join(
        (message.text or "")
        for message in messages
        if message.direction is MessageDirection.INCOMING
    )
    customer_folded = normalize_for_matching(customer_text)
    merchant_folded = normalize_for_matching(" ".join(recent_merchant_replies))
    has_vehicle = any(marker in customer_folded for marker in _VEHICLE_MARKERS)
    has_range = any(marker in customer_folded for marker in _RANGE_MARKERS)
    asked_vehicle = "二轮还是三轮" in merchant_folded
    asked_range = any(
        marker in merchant_folded
        for marker in ("想跑多少公里", "跑多少公里", "通勤还是跑长途")
    )
    qualification_already_asked = asked_vehicle or asked_range

    if proposal.intent == "battery_catalog":
        return SalesPlan(proposal, SalesStage.RECOMMEND)

    sales_question = proposal.intent in {
        "price",
        "price_inquiry",
        "recommendation",
        "battery_recommendation",
    } or any(marker in folded for marker in _SALES_QUESTION_MARKERS)
    customer_asks_fit_or_range = any(
        marker in folded
        for marker in ("二轮", "三轮", "车型", "适配", "能装", "能用", "续航", "跑多远", "公里")
    )
    if not sales_question or not customer_asks_fit_or_range or qualification_already_asked:
        return SalesPlan(proposal, SalesStage.PAUSED)

    if knowledge is None:
        if not has_vehicle and not asked_vehicle:
            nudge = "你是二轮还是三轮？平时想跑多少公里"
            follow_up = "方便说下车型和想跑的公里数，我给你配合适的"
        elif not has_range and not asked_range:
            nudge = "平时想跑多少公里？我按用途帮你看"
            follow_up = "你平时主要通勤还是跑长途？我按续航帮你看"
        else:
            return SalesPlan(proposal, SalesStage.PAUSED)
        enhanced = _append_once(proposal, nudge, recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            follow_up,
        )

    if (
        not has_vehicle
        and not asked_vehicle
        and not _QUESTION_RE.search(proposal.reply_text)
    ):
        nudge = "你平时用什么车型？我帮你核下适配"
        enhanced = _append_once(proposal, nudge, recent_merchant_replies)
        return SalesPlan(
            enhanced,
            SalesStage.QUALIFY,
            "方便说下车型和想跑的公里数，我再帮你核下适配",
        )
    if not has_range and not asked_range and not _QUESTION_RE.search(proposal.reply_text):
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

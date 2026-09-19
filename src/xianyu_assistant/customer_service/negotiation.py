"""Deterministic negotiation decisions; the model may only render the wording."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from xianyu_assistant.customer_service.models import ProductKnowledge, ReplyProposal
from xianyu_assistant.customer_service.price_change import format_money, parse_money
from xianyu_assistant.customer_service.semantic_analysis import CustomerSemantics

_MODEL_NUMBER_RE = re.compile(r"(?<!\d)(?:(?:48|60)\d{2})(?!\d)")
_VOLTAGE_CAPACITY_RE = re.compile(
    r"(?<!\d)\d+(?:\.\d+)?\s*(?:ah|安时|v|伏|a|安|km|公里|w|瓦|%|厘米|cm|mm)",
    re.IGNORECASE,
)
_DIMENSION_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?(?:\s*[-x×*]\s*\d+(?:\.\d+)?){1,3}(?!\d)")
_YEAR_OR_HEALTH_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?\s*(?:年|健康度|以上)")
_MONEY_UNIT_RE = re.compile(
    r"(?:[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块钱|块))"
)
_QUANTITY_RE = re.compile(r"(?<!\d)(\d+|[一二两三四五六七八九十两]+)\s*(?:组|套|只|个)(?!人)")
_MONEY_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")
_NEGOTIATION_MARKERS = (
    "最低",
    "便宜",
    "优惠",
    "少点",
    "让点",
    "砍价",
    "到手价",
    "实价",
    "诚心要",
    "卖不卖",
    "能卖",
    "能出",
    "可以出",
    "可以吗",
    "可以不",
    "行不行",
    "行吗",
    "别人",
    "别家",
)
_ORDER_MARKERS = (
    "拍下",
    "拍了",
    "已拍",
    "下单",
    "待付款",
    "等付款",
    "改价",
    "修改价格",
)
_CONDITIONAL_PURCHASE_MARKERS = (
    "可以的话",
    "行的话",
    "能的话",
    "我就拍",
    "就拍下",
)
_PURCHASE_HOWTO_MARKERS = (
    "怎么拍下",
    "怎么拍",
    "如何拍下",
    "如何拍",
    "怎么下单",
    "如何下单",
    "在哪拍",
    "哪里拍",
)
_PRICE_QUERY_MARKERS = ("多少钱", "多钱", "什么价", "啥价", "价格", "价钱", "怎么卖", "咋卖")
_REJECTION_MARKERS = (
    "不行",
    "出不了",
    "做不到",
    "做不了",
    "改不了",
    "不能",
    "不卖",
    "少不了",
)
_REPLY_COMPARISON_RE = re.compile(r"[\s，。！？、,.!?:：；;]+")
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True, slots=True)
class NegotiationState:
    product_key: str
    quantity: int = 1
    last_customer_offer: str | None = None
    last_counter: str | None = None
    accepted_price: str | None = None
    outcome: str | None = None
    repeated_offer_count: int = 0


@dataclass(frozen=True, slots=True)
class NegotiationPlan:
    outcome: str
    product_key: str
    quantity: int
    price_basis: str
    customer_offer: str | None
    reply_price: str | None
    accepted_price: str | None
    listed_total: str
    minimum_total: str
    is_order_request: bool
    instruction: str
    fallback_reply: str
    next_state: NegotiationState

    @property
    def requires_handoff(self) -> bool:
        """Multi-item order price changes need a human unit/total verification."""
        return self.is_order_request and self.quantity > 1

    @property
    def allowed_numbers(self) -> tuple[str, ...]:
        values = (
            self.customer_offer,
            self.reply_price,
            self.accepted_price,
            self.listed_total,
            self.minimum_total,
            str(self.quantity),
        )
        return tuple(value for value in values if value is not None)

    def as_prompt_data(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "quantity": self.quantity,
            "price_basis": self.price_basis,
            "customer_offer": self.customer_offer,
            "reply_price": self.reply_price,
            "accepted_price": self.accepted_price,
            "is_order_request": self.is_order_request,
            "repeated_offer_count": self.next_state.repeated_offer_count,
            "instruction": self.instruction,
            "contract": (
                "outcome、金额和接受/拒绝结论均由本地程序确定，不得更改。"
                "不得说“最低价”或补充未列出的商品事实。"
            ),
        }


def _plain_money(value: Decimal) -> str:
    fixed = format_money(value)
    return fixed[:-3] if fixed.endswith(".00") else fixed.rstrip("0").rstrip(".")


def _parse_chinese_quantity(value: str) -> int | None:
    if value.isdigit():
        number = int(value)
        return number if 1 <= number <= 99 else None
    if value in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[value]
    if value.startswith("十") and len(value) == 2:
        return 10 + _CHINESE_NUMBERS.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return _CHINESE_NUMBERS.get(value[0], 0) * 10
    if "十" in value and len(value) == 3:
        left, right = value.split("十", 1)
        return _CHINESE_NUMBERS.get(left, 0) * 10 + _CHINESE_NUMBERS.get(right, 0)
    return None


def quantity_from_messages(current_query: str, prior_customer_texts: tuple[str, ...]) -> int:
    for text in (current_query, *reversed(prior_customer_texts)):
        match = _QUANTITY_RE.search(text)
        if match is None:
            continue
        quantity = _parse_chinese_quantity(match.group(1))
        if quantity is not None:
            return quantity
    return 1


def _offer_from_query(query: str, *, allow_contextual_bare: bool = False) -> Decimal | None:
    explicit = list(_MONEY_UNIT_RE.finditer(query))
    if explicit:
        return Decimal(explicit[-1].group(1) or explicit[-1].group(2))
    cleaned = _MODEL_NUMBER_RE.sub(" ", query)
    cleaned = _VOLTAGE_CAPACITY_RE.sub(" ", cleaned)
    cleaned = _DIMENSION_RE.sub(" ", cleaned)
    cleaned = _YEAR_OR_HEALTH_RE.sub(" ", cleaned)
    quantity_match = _QUANTITY_RE.search(cleaned)
    if quantity_match is not None:
        start, end = quantity_match.span(1)
        cleaned = cleaned[:start] + " " * (end - start) + cleaned[end:]
    has_price_language = _is_negotiation_text(query) or any(
        marker in query for marker in _ORDER_MARKERS
    )
    if not has_price_language and not allow_contextual_bare:
        return None
    candidates: list[Decimal] = []
    for match in _MONEY_RE.finditer(cleaned):
        amount = Decimal(match.group(1))
        if amount >= Decimal(20):
            candidates.append(amount)
    return candidates[-1] if candidates else None


def _explicit_total_basis(query: str, quantity: int) -> bool:
    if quantity <= 1:
        return True
    if any(marker in query for marker in ("总价", "一共", "合计")):
        return True
    return bool(re.search(r"(?:组|套|只|个)\s*\d", query))


def _is_negotiation_text(query: str) -> bool:
    return any(marker in query for marker in _NEGOTIATION_MARKERS)


def build_negotiation_plan(
    query: str,
    knowledge: ProductKnowledge | None,
    *,
    quantity: int,
    opening_counter: str | None,
    previous: NegotiationState | None,
    semantics: CustomerSemantics | None = None,
) -> NegotiationPlan | None:
    if knowledge is None or not knowledge.listed_price or not knowledge.minimum_price:
        return None
    same_previous = previous if previous and previous.product_key == knowledge.product_key else None
    offer = (
        Decimal(semantics.money_offer)
        if semantics is not None and semantics.is_negotiation and semantics.money_offer
        else _offer_from_query(query, allow_contextual_bare=same_previous is not None)
    )
    is_purchase_howto = any(marker in query for marker in _PURCHASE_HOWTO_MARKERS)
    is_order_request = (
        any(marker in query for marker in _ORDER_MARKERS) and not is_purchase_howto
    )
    is_conditional_purchase = any(marker in query for marker in _CONDITIONAL_PURCHASE_MARKERS)
    asks_negotiation = _is_negotiation_text(query)
    asks_quantity_price = quantity > 1 and any(marker in query for marker in _PRICE_QUERY_MARKERS)
    if (
        offer is None
        and same_previous is not None
        and same_previous.outcome == "clarify"
        and same_previous.last_customer_offer is not None
        and any(marker in query for marker in ("总价", "单价", "一共", "合计"))
    ):
        offer = Decimal(same_previous.last_customer_offer)
    if (
        offer is None
        and not asks_negotiation
        and not is_conditional_purchase
        and not (is_purchase_howto and same_previous is not None)
        and not asks_quantity_price
        and not (is_order_request and same_previous is not None)
    ):
        return None

    listed = parse_money(knowledge.listed_price, field_name="标准成交价")
    minimum = parse_money(knowledge.minimum_price, field_name="最低成交价")
    opening = parse_money(opening_counter or knowledge.listed_price, field_name="首次还价")
    quantity_decimal = Decimal(quantity)
    listed_total = listed * quantity_decimal
    minimum_total = minimum * quantity_decimal
    opening_total = max(minimum_total, min(listed_total, opening * quantity_decimal))

    if offer is None and asks_quantity_price:
        listed_total_text = _plain_money(listed_total)
        state = NegotiationState(
            knowledge.product_key,
            quantity,
            None,
            listed_total_text,
            None,
            "quote",
        )
        return NegotiationPlan(
            "quote",
            knowledge.product_key,
            quantity,
            "total",
            None,
            listed_total_text,
            None,
            listed_total_text,
            _plain_money(minimum_total),
            is_order_request,
            f"顾客询问{quantity}组正常总价，按单价合计为{listed_total_text}。",
            f"{quantity}组{listed_total_text} 包邮",
            state,
        )

    if offer is None and same_previous is not None:
        if same_previous.outcome == "accept" and (
            is_conditional_purchase or is_purchase_howto or is_order_request
        ):
            accepted = same_previous.accepted_price
            assert accepted is not None
            state = NegotiationState(
                knowledge.product_key,
                quantity,
                same_previous.last_customer_offer,
                same_previous.last_counter,
                accepted,
                "accept",
            )
            return NegotiationPlan(
                "accept",
                knowledge.product_key,
                quantity,
                "total",
                same_previous.last_customer_offer,
                accepted,
                accepted,
                _plain_money(listed_total),
                _plain_money(minimum_total),
                is_order_request,
                f"确认此前已接受的成交价{accepted}，简短告诉顾客如何继续拍下。",
                (
                    f"直接拍下就行 还是按{accepted}"
                    if is_purchase_howto
                    else f"可以 {accepted}拍下"
                ),
                state,
            )
        counter = same_previous.last_counter or _plain_money(opening_total)
        state = NegotiationState(
            knowledge.product_key,
            quantity,
            same_previous.last_customer_offer,
            counter,
            None,
            "reject",
        )
        return NegotiationPlan(
            "reject",
            knowledge.product_key,
            quantity,
            "total",
            same_previous.last_customer_offer,
            counter,
            None,
            _plain_money(listed_total),
            _plain_money(minimum_total),
            is_order_request,
            f"此前报价未接受，只能按{counter}继续谈，不得表示已同意。",
            f"按{counter}可以",
            state,
        )

    if offer is None:
        counter = _plain_money(opening_total)
        state = NegotiationState(knowledge.product_key, quantity, None, counter, None, "quote")
        return NegotiationPlan(
            "quote",
            knowledge.product_key,
            quantity,
            "total",
            None,
            counter,
            None,
            _plain_money(listed_total),
            _plain_money(minimum_total),
            is_order_request,
            f"顾客只要求优惠，首次还价为{counter}。",
            f"{counter}可以",
            state,
        )

    explicit_total = _explicit_total_basis(query, quantity)
    if quantity > 1 and not explicit_total:
        offer_text = _plain_money(offer)
        state = NegotiationState(knowledge.product_key, quantity, offer_text, None, None, "clarify")
        return NegotiationPlan(
            "clarify",
            knowledge.product_key,
            quantity,
            "ambiguous",
            offer_text,
            None,
            None,
            _plain_money(listed_total),
            _plain_money(minimum_total),
            is_order_request,
            "报价基准不明确，只确认这是多件总价还是单价，不判断接受或拒绝。",
            f"{offer_text}是{quantity}组总价吗",
            state,
        )

    basis = "total" if explicit_total else "unit"
    offer_total = offer if basis == "total" else offer * quantity_decimal
    offer_display = _plain_money(offer)
    if offer_total > listed_total:
        reply_price = _plain_money(listed_total if basis == "total" else listed)
        state = NegotiationState(
            knowledge.product_key, quantity, offer_display, reply_price, None, "quote"
        )
        return NegotiationPlan(
            "quote",
            knowledge.product_key,
            quantity,
            basis,
            offer_display,
            reply_price,
            None,
            _plain_money(listed_total),
            _plain_money(minimum_total),
            is_order_request,
            f"顾客报价高于标价，按正常价格{reply_price}回复。",
            f"不用那么多 {reply_price}就行",
            state,
        )
    if offer_total >= minimum_total:
        accepted = offer_display
        state = NegotiationState(
            knowledge.product_key, quantity, offer_display, None, accepted, "accept"
        )
        return NegotiationPlan(
            "accept",
            knowledge.product_key,
            quantity,
            basis,
            offer_display,
            offer_display,
            accepted,
            _plain_money(listed_total),
            _plain_money(minimum_total),
            is_order_request,
            f"顾客报价{offer_display}在可接受区间内，必须明确接受。",
            f"{offer_display}可以",
            state,
        )

    previous_offer = (
        Decimal(same_previous.last_customer_offer)
        if same_previous and same_previous.last_customer_offer
        else None
    )
    if (
        same_previous is not None
        and same_previous.last_counter is not None
        and previous_offer is not None
        and offer > previous_offer
    ):
        counter_amount = minimum_total if basis == "total" else minimum
    else:
        counter_amount = opening_total if basis == "total" else opening
    counter = _plain_money(counter_amount)
    repeated_offer_count = (
        same_previous.repeated_offer_count + 1
        if same_previous is not None
        and same_previous.last_customer_offer == offer_display
        else 0
    )
    state = NegotiationState(
        knowledge.product_key,
        quantity,
        offer_display,
        counter,
        None,
        "reject",
        repeated_offer_count,
    )
    if repeated_offer_count and is_order_request:
        instruction = (
            f"顾客再次坚持此前未接受的报价{offer_display}，并表示已拍下。"
            f"不要重复上一轮整句；简短说明拍下也不能按该价格，只能按{counter}修改。"
        )
        fallback_reply = f"拍了也不行 只能按{counter}改"
    elif repeated_offer_count == 1:
        instruction = (
            f"顾客重复此前未接受的报价{offer_display}。不要照抄上一轮；"
            f"不必再次复述顾客报价，简短坚持{counter}。"
        )
        fallback_reply = f"这个价做不了 {counter}可以"
    elif repeated_offer_count > 1:
        instruction = (
            f"顾客已多次重复低于可接受区间的报价。不要重复前文，"
            f"只用更短的话坚持{counter}。"
        )
        fallback_reply = f"按{counter}来 可以就拍"
    else:
        instruction = f"顾客报价{offer_display}低于可接受区间，必须拒绝，并还价{counter}。"
        fallback_reply = f"{offer_display}不行 {counter}可以"
    return NegotiationPlan(
        "reject",
        knowledge.product_key,
        quantity,
        basis,
        offer_display,
        counter,
        None,
        _plain_money(listed_total),
        _plain_money(minimum_total),
        is_order_request,
        instruction,
        fallback_reply,
        state,
    )


def validate_negotiation_reply(
    proposal: ReplyProposal,
    plan: NegotiationPlan,
    *,
    recent_merchant_replies: Sequence[str] = (),
) -> None:
    text = proposal.reply_text.strip()
    rejected = any(marker in text for marker in _REJECTION_MARKERS)
    if "最低" in text:
        raise ValueError("议价回复不得主动使用“最低”暴露内部边界。")
    if plan.outcome == "accept" and rejected:
        raise ValueError("本地议价结论为接受，模型却表示拒绝。")
    if plan.outcome == "reject" and not rejected and not text.startswith("按"):
        raise ValueError("本地议价结论为拒绝，模型没有明确拒绝。")
    if plan.outcome == "clarify" and not any(marker in text for marker in ("总价", "单价")):
        raise ValueError("多件报价基准不明确时必须追问总价或单价。")
    if plan.reply_price is not None and plan.reply_price not in text:
        raise ValueError(f"议价回复缺少本地确定金额 {plan.reply_price}。")
    if plan.is_order_request and plan.outcome == "accept":
        if proposal.offered_price != plan.accepted_price:
            raise ValueError("改价建议必须等于本地已经接受的成交价。")
    elif proposal.offered_price is not None:
        raise ValueError("未满足拍下改价条件时不得生成改价建议。")
    canonical = _REPLY_COMPARISON_RE.sub("", text).casefold()
    if len(canonical) >= 6 and any(
        _REPLY_COMPARISON_RE.sub("", previous).casefold() == canonical
        for previous in recent_merchant_replies[-4:]
    ):
        raise ValueError("议价回复与最近商家回复完全重复，必须保持价格结论但更换表达。")

"""Shared, deterministic order and first-contact rules; no model or browser calls."""
from __future__ import annotations

import re
from dataclasses import dataclass

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching
from xianyu_assistant.customer_service.models import ConversationSnapshot, MessageDirection

CONTACT_POLICY_VERSION = "welcome-order-v2"
ORDER_RANK = {"pending_payment": 1, "paid": 2, "pending_shipment": 2,
              "shipped": 3, "delivered": 4, "refund": 5, "cancelled": 5}
_NEGATED = re.compile(r"(?:没|未|不|还没|尚未|没有|还未)\s*(?:有)?\s*(?:拍下|拍|下单|付款|支付)")
_ORDER_PATTERNS = (
    ("refund", r"(?:我(?:已|已经)?(?:发起|申请)了?退款|退款中|退款申请|交易关闭|订单已退款)"),
    ("cancelled", r"(?:订单已取消|已取消订单|取消了订单)"),
    ("delivered", r"(?:订单已签收|买家已签收|确认收货成功)"),
    ("shipped", r"(?:你已发货|卖家已发货|订单已发货)"),
    ("paid", r"(?:已付款|已支付|(?:我)?已经付款|(?:我)?付款了|付好了|支付成功|买家已付款|等待(?:你|卖家)发货|待发货)"),
    ("pending_payment", r"(?:我已拍下|(?:我)?已经拍下|(?:我)?已拍下|(?:我)?拍下了|我拍了|已经拍了|(?:我)?下单了|已下单|待付款|等待(?:买家|你)付款|等你改价)"),
)


@dataclass(frozen=True)
class OrderEvidence:
    state: str
    source: str
    text: str
    message_key: str = ""
    event_time: str | None = None
    order_id: str | None = None

    def as_data(self) -> dict[str, object]:
        return {"state": self.state, "source": self.source, "text": self.text,
                "message_key": self.message_key, "event_time": self.event_time,
                "order_id": self.order_id}


def order_state_from_text(text: str) -> str | None:
    folded = normalize_for_matching(text)
    states = []
    # Evaluate clauses independently so "没付款，但我已拍下" keeps the second fact.
    for clause in re.split(r"[，,。;；\n]|但是|但", folded):
        if _NEGATED.search(clause) or re.search(r"(?:是否|有没有|如果|假如|还没有|还没).*(?:拍|付款|支付)", clause):
            continue
        for state, pattern in _ORDER_PATTERNS:
            if re.search(pattern, clause) and not re.search(r"(?:已付款|付款了|已拍下|拍下了|下单了)\s*[吗么?？]", clause):
                states.append(state)
    return max(states, key=ORDER_RANK.__getitem__) if states else None


def is_order_card(text: str) -> bool:
    return bool(order_state_from_text(text) and any(marker in text for marker in (
        "请双方沟通", "及时确认价格", "等待你处理", "请确认操作", "等待你发货", "等待卖家发货",
        "我已修改价格", "卖家已修改价格",
    )))


def order_evidence(snapshot: ConversationSnapshot) -> tuple[OrderEvidence, ...]:
    result = []
    # Header describes the current order and outranks old timeline cards.
    for message in snapshot.messages:
        if message.direction is not MessageDirection.INCOMING:
            continue
        state = order_state_from_text(message.text or "")
        if state:
            result.append(OrderEvidence(state, "order_card" if is_order_card(message.text or "") else "customer_statement",
                                        message.text or "", message.message_key,
                                        message.platform_time.isoformat() if message.platform_time else None))
    for event in snapshot.system_events:
        state = order_state_from_text(event.text)
        if state:
            result.append(OrderEvidence(state, "platform_event", event.text, event.event_key))
    state = order_state_from_text(snapshot.order_state_text or "")
    if state:
        result.append(OrderEvidence(state, "platform_header", snapshot.order_state_text or "",
                                    order_id=snapshot.platform_order_id))
    return tuple(result)


def order_acknowledgement(query: str) -> str | None:
    state = order_state_from_text(query)
    if state == "pending_payment":
        return "看到你拍下了，我核对下这单价格"
    if state in {"paid", "pending_shipment"}:
        return "收到，我核对下订单"
    return None


def explicitly_requests_catalog(query: str) -> bool:
    folded = normalize_for_matching(query)
    return not is_order_card(query) and any(marker in folded for marker in (
        "价格表", "价目表", "型号价格", "有哪些型号", "有什么型号", "再发目录", "发下目录", "看看目录",
        "报价再发", "再发报价", "再发一次目录", "重新发报价", "重发目录",
    ))


def looks_like_full_catalog(text: str) -> bool:
    from xianyu_assistant.customer_service.battery_models import model_matches
    return len({(m[1] or m[3], m[2] or m[4]) for m in model_matches(text)}) >= 2 and "容量" in text and "健康" in text


def contains_contact_template(text: str, templates: tuple[str, ...]) -> bool:
    """Match a complete configured component, retaining prices and other facts."""
    compact = "".join(text.split())
    return any("".join(template.split()) in compact for template in templates
               if len("".join(template.split())) >= 12)


def catalog_component(text: str) -> str | None:
    """Recover historical catalog paragraphs, excluding appended answers."""
    if not looks_like_full_catalog(text):
        return None
    from xianyu_assistant.customer_service.battery_models import model_matches
    blocks = [p.strip() for p in re.split(r"\n\s*\n", text)
              if len({(m[1] or m[3], m[2] or m[4]) for m in model_matches(p)}) == 1 and "容量" in p and "健康" in p
              and re.search(r"\d+(?:\.\d+)?\s*元|[¥￥]\s*\d", p)]
    return "\n\n".join(blocks) if len(blocks) >= 2 else text.strip()


def quoted_customer_question(text: str, templates: tuple[str, ...]) -> str:
    """Retain a separate buyer question without treating the quoted component as an ask."""
    remaining = strip_fixed_components(text, tuple(t for t in templates if len("".join(t.split())) >= 12))
    if remaining == text:
        return text
    return remaining.strip(' \n\r“”"「」『』>')


def strip_fixed_components(text: str, templates: tuple[str, ...]) -> str:
    """Strip only whole configured components, wherever attached, retaining normal answers."""
    result = text
    for template in templates:
        if not template or not template.strip():
            continue
        pattern = r"\s+".join(re.escape(part) for part in template.strip().split())
        result = re.sub(pattern, "", result).strip()
    return result


def guarded_reply(text: str, *, allowed: bool, templates: tuple[str, ...], query: str,
                  known_order: bool, requested_catalog: bool) -> tuple[str, bool]:
    """Independent final guard, including old/restored/edited drafts lacking flags."""
    if requested_catalog:
        return text, False
    cleaned = text if allowed else strip_fixed_components(text, templates)
    blocked = cleaned != text
    if looks_like_full_catalog(cleaned) and (not allowed or known_order):
        cleaned, blocked = "", True
    if blocked and not cleaned:
        cleaned = order_acknowledgement(query) or ("这单我核对下，你具体想确认哪项？" if known_order else "你要哪个型号？我按型号报实价")
    return cleaned, blocked

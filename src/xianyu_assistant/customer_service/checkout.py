"""Order-card boundaries and trusted checkout commitments (no page/model calls)."""
from __future__ import annotations

from typing import Any

from xianyu_assistant.customer_service.contact_policy import is_order_card, order_state_from_text
from xianyu_assistant.customer_service.fingerprints import content_fingerprint
from xianyu_assistant.customer_service.models import ConversationSnapshot, MessageDirection


def normalized_order_text(text: str) -> str:
    if "我已修改价格" in text or "卖家已修改价格" in text:
        return "订单价格已修改，等待付款"
    if not is_order_card(text):
        return text
    return {"pending_payment": "我已拍下，待付款", "paid": "我已付款，等待发货",
            "refund": "我发起了退款申请"}.get(order_state_from_text(text), "订单状态通知")


def current_order_state(snapshot: ConversationSnapshot) -> str | None:
    """Current platform header wins; old customer statements are not current orders."""
    state = order_state_from_text(snapshot.order_state_text or "")
    if state:
        return state
    for message in reversed(snapshot.messages):
        if is_order_card(message.text or ""):
            return order_state_from_text(message.text or "")
    return None


def price_already_changed(snapshot: ConversationSnapshot) -> bool:
    """A later platform price-change receipt supersedes the previous pending card."""
    for message in reversed(snapshot.messages):
        text = message.text or ""
        if "我已修改价格" in text or "卖家已修改价格" in text:
            return True
        if message.direction is MessageDirection.INCOMING and is_order_card(text):
            return False
    return False


def order_binding(snapshot: ConversationSnapshot) -> dict[str, Any]:
    result: dict[str, Any] = {
        "conversation_key": snapshot.conversation_key,
        "merchant_account_id": snapshot.merchant_account_id,
        "platform_customer_id": snapshot.platform_customer_id,
        "product_id": snapshot.platform_product_id,
        "order_id": snapshot.platform_order_id,
    }
    # A platform card can bind a pending order when the header has no order id.
    # Position-only identities may change on scroll and cannot authorize money.
    cards = [m for m in snapshot.messages if m.direction is MessageDirection.INCOMING
             and is_order_card(m.text or "")
             and order_state_from_text(m.text or "") == "pending_payment"]
    if cards:
        card = cards[-1]
        if (card.key_source in {"platform_attribute", "react_message_id", "content_fallback"}
                and sum(m.message_key == card.message_key for m in cards) == 1):
            result.update(order_card_message_key=card.message_key,
                          order_card_key_source=card.key_source)
    return result


def reliable_order_binding(evidence: dict[str, Any]) -> bool:
    return bool(all(evidence.get(k) for k in (
        "conversation_key", "merchant_account_id", "platform_customer_id", "product_id",
    )) and (evidence.get("order_id") or (
        evidence.get("order_card_message_key") and evidence.get("order_card_key_source")
        in {"platform_attribute", "react_message_id", "content_fallback"})))


def binding_matches(snapshot: ConversationSnapshot, evidence: dict[str, Any]) -> bool:
    current = order_binding(snapshot)
    keys = ("conversation_key", "merchant_account_id", "platform_customer_id", "product_id")
    if not reliable_order_binding(evidence) or any(current[k] != evidence[k] for k in keys):
        return False
    if evidence.get("order_id"):
        return current["order_id"] == evidence["order_id"]
    return (current.get("order_card_message_key") == evidence.get("order_card_message_key")
            and current.get("order_card_key_source") == evidence.get("order_card_key_source"))


def approved_binding_matches(evidence: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Close the race between task approval in the parent and final page confirmation."""
    return bool(reliable_order_binding(expected) and reliable_order_binding(evidence)
                and expected.get("last_customer_key")
                and all(evidence.get(key) == value for key, value in expected.items() if value is not None))


def checkout_task_id(conversation_key: str, send_id: str) -> str:
    """One verified single-item commitment cannot authorize multiple price submissions."""
    return "pc-" + content_fingerprint(conversation_key, "checkout", send_id)


def checkout_intent_matches(intent: dict[str, Any], snapshot: ConversationSnapshot,
                            *, product_key: str, settings_version: str | None,
                            price_variant: str, quantity: int) -> bool:
    return bool(quantity == 1 and intent.get("quantity") == 1
                and intent.get("product_key") == product_key
                and intent.get("settings_version") == settings_version
                and intent.get("price_variant") == price_variant
                and all(intent.get(k) and intent[k] == getattr(snapshot, k) for k in (
                    "conversation_key", "merchant_account_id", "platform_customer_id", "platform_product_id",
                )))

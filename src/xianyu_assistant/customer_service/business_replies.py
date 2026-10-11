"""Operator-editable wording for a small set of confirmed, non-price facts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessReply:
    key: str
    label: str
    default: str
    required: tuple[str, ...]


BUSINESS_REPLIES = (
    BusinessReply("pickup", "自提地点", "广东普宁自提", ("广东普宁", "自提")),
    BusinessReply("shipping_origin", "发货地点", "广东普宁发", ("广东普宁", "发")),
    BusinessReply("default_carriers", "默认物流", "默认发安能物流和京东", ("安能物流", "京东")),
    BusinessReply("charger", "赠送充电器", "默认送充电器", ("送", "充电器")),
    BusinessReply("first_use", "首次使用", "到货先充满电 再装车", ("充满", "装车")),
)
BUSINESS_REPLY_SETTING_PREFIX = "business_reply_"


def validate_business_reply(reply: BusinessReply, value: str) -> str:
    text = value.strip()
    if not text or len(text) > 100 or "\n" in text:
        raise ValueError(f"{reply.label}话术须为 1–100 字的单行文本。")
    if any(anchor not in text for anchor in reply.required):
        raise ValueError(f"{reply.label}话术必须保留已确认的事实：{'、'.join(reply.required)}。")
    if any(marker in text for marker in ("微信", "加我", "私下", "转账", "支付宝", "退款", "质保", "包退", "元", "¥", "￥")):
        raise ValueError(f"{reply.label}话术不能包含付款、价格、售后或站外联系承诺。")
    return text


def configured_business_reply(repository: object | None, key: str) -> str:
    reply = next(item for item in BUSINESS_REPLIES if item.key == key)
    get_setting = getattr(repository, "get_setting", None)
    if not callable(get_setting):
        return reply.default
    try:
        return validate_business_reply(
            reply, str(get_setting(BUSINESS_REPLY_SETTING_PREFIX + key, reply.default) or "")
        )
    except (ValueError, TypeError, RuntimeError):
        return reply.default

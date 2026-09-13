"""Fixed-point local policy for financially sensitive order-price changes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from xianyu_assistant.customer_service.models import ProductKnowledge

_CENT = Decimal("0.01")
_MAX_PRICE = Decimal("99999999.99")


class PricePolicyError(ValueError):
    """Raised when a proposed price cannot safely become an executable action."""


@dataclass(frozen=True, slots=True)
class PriceDecision:
    approved_price: str
    minimum_price: str
    listed_price: str | None


def parse_money(value: str, *, field_name: str = "价格") -> Decimal:
    """Parse a positive monetary value without binary floating-point rounding."""
    cleaned = value.strip().replace("¥", "").replace("￥", "").replace("元", "")
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError) as error:
        raise PricePolicyError(f"{field_name}不是有效金额。") from error
    if not amount.is_finite() or amount <= 0 or amount > _MAX_PRICE:
        raise PricePolicyError(f"{field_name}必须是有效的正数金额。")
    if amount.as_tuple().exponent < -2:
        raise PricePolicyError(f"{field_name}最多保留两位小数。")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def parse_nonnegative_money(value: str, *, field_name: str) -> Decimal:
    """Parse a monetary field such as shipping where an exact zero is valid."""
    cleaned = value.strip().replace("¥", "").replace("￥", "").replace("元", "")
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError) as error:
        raise PricePolicyError(f"{field_name}不是有效金额。") from error
    if not amount.is_finite() or amount < 0 or amount > _MAX_PRICE:
        raise PricePolicyError(f"{field_name}必须是有效的非负金额。")
    if amount.as_tuple().exponent < -2:
        raise PricePolicyError(f"{field_name}最多保留两位小数。")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def format_money(amount: Decimal) -> str:
    """Return the exact two-decimal string expected by the Xianyu form."""
    return format(amount.quantize(_CENT, rounding=ROUND_HALF_UP), ".2f")


def check_price_change(proposed_price: str, knowledge: ProductKnowledge) -> PriceDecision:
    """Authorize a proposal only when the catalog has an explicit price floor."""
    if not knowledge.minimum_price:
        raise PricePolicyError("商品未设置最低成交价，不能执行改价。")
    if not knowledge.listed_price:
        raise PricePolicyError("商品未设置标准成交价，不能执行改价。")
    proposed = parse_money(proposed_price, field_name="建议改价")
    minimum = parse_money(knowledge.minimum_price, field_name="最低成交价")
    listed_amount = parse_money(knowledge.listed_price, field_name="标准成交价")
    if minimum > listed_amount:
        raise PricePolicyError("最低成交价高于标准成交价，请先修正商品表。")
    if proposed < minimum:
        raise PricePolicyError(
            f"建议改价 {format_money(proposed)} 低于最低成交价 {format_money(minimum)}。"
        )
    if proposed > listed_amount:
        raise PricePolicyError(
            f"建议改价 {format_money(proposed)} 高于标准成交价 {format_money(listed_amount)}。"
        )
    listed = format_money(listed_amount)
    return PriceDecision(format_money(proposed), format_money(minimum), listed)

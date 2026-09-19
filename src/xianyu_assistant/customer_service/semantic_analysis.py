"""Structured customer-turn semantics with deterministic numeric safeguards.

The language model may resolve genuinely ambiguous wording, but unit-bound
numbers and negotiation eligibility are always checked locally.  A failed or
invalid model analysis therefore degrades to clarification/non-negotiation,
never to an invented price offer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching

_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")
_RANGE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:km|公里)", re.IGNORECASE)
_POWER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:w|瓦)", re.IGNORECASE)
_VOLTAGE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:v|伏)", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:ah|安时|a|安)", re.IGNORECASE)
_MONEY_UNIT_RE = re.compile(
    r"(?:[¥￥]\s*(?P<prefix>\d+(?:\.\d+)?)|(?P<suffix>\d+(?:\.\d+)?)\s*(?:元|块钱|块))",
    re.IGNORECASE,
)
_COMPACT_BATTERY_RE = re.compile(r"(?<!\d)((?:48|60)\d{2})(?!\d)")
_PURE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:元|块)?\s*[？?。!！]*\s*$")
_QUANTITY_RE = re.compile(r"(?<!\d)(\d+)\s*(?:组|套|只|个)(?!人)")

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
_PRICE_QUESTION_MARKERS = (
    "多少钱",
    "多钱",
    "什么价",
    "啥价",
    "价格",
    "价钱",
    "怎么卖",
    "咋卖",
)
_ORDER_MARKERS = ("拍下", "拍了", "已拍", "下单", "待付款", "等付款", "改价")
_CORRECTION_MARKERS = ("不是", "不要", "改成", "换成", "说错", "更正")


@dataclass(frozen=True, slots=True)
class CustomerSemantics:
    """Machine-readable meaning of the current complete customer turn."""

    primary_intent: str
    battery_model: str | None = None
    motor_power_w: str | None = None
    required_range_km: str | None = None
    quantity: int = 1
    money_offer: str | None = None
    is_negotiation: bool = False
    is_order_request: bool = False
    is_correction: bool = False
    questions: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    confidence: float = 1.0
    evidence: tuple[tuple[str, str], ...] = ()
    needs_model_resolution: bool = False

    def as_prompt_data(self) -> dict[str, object]:
        return {
            "primary_intent": self.primary_intent,
            "entities": {
                "battery_model": self.battery_model,
                "motor_power_w": self.motor_power_w,
                "required_range_km": self.required_range_km,
                "quantity": self.quantity,
                "money_offer": self.money_offer,
            },
            "is_negotiation": self.is_negotiation,
            "is_order_request": self.is_order_request,
            "is_correction": self.is_correction,
            "questions": list(self.questions),
            "ambiguities": list(self.ambiguities),
            "confidence": self.confidence,
            "evidence": [
                {"text": text, "type": kind} for text, kind in self.evidence
            ],
        }


def analyze_customer_turn(
    text: str,
    *,
    negotiation_active: bool = False,
) -> CustomerSemantics:
    """Classify explicit units and decide whether model disambiguation is needed."""
    folded = normalize_for_matching(text)
    evidence: list[tuple[str, str]] = []
    occupied: list[tuple[int, int]] = []

    def collect(pattern: re.Pattern[str], kind: str) -> list[re.Match[str]]:
        matches = list(pattern.finditer(folded))
        for match in matches:
            occupied.append(match.span())
            evidence.append((match.group(0), kind))
        return matches

    ranges = collect(_RANGE_RE, "required_range_km")
    powers = collect(_POWER_RE, "motor_power_w")
    voltages = collect(_VOLTAGE_RE, "voltage")
    capacities = collect(_CAPACITY_RE, "capacity")
    compact_models = collect(_COMPACT_BATTERY_RE, "battery_model")
    quantities = collect(_QUANTITY_RE, "quantity")
    money_units = collect(_MONEY_UNIT_RE, "money_offer")

    battery_model: str | None = None
    if compact_models:
        raw = compact_models[-1].group(1)
        battery_model = f"{raw[:2]}V{raw[2:]}Ah"
    elif voltages and capacities:
        battery_model = (
            f"{_plain_number(voltages[-1].group(1))}V"
            f"{_plain_number(capacities[-1].group(1))}Ah"
        )

    explicit_money: str | None = None
    if money_units:
        match = money_units[-1]
        explicit_money = _plain_number(match.group("prefix") or match.group("suffix"))

    untyped: list[str] = []
    for match in _NUMBER_RE.finditer(folded):
        if any(start <= match.start() and match.end() <= end for start, end in occupied):
            continue
        untyped.append(match.group(1))

    has_negotiation_marker = any(marker in folded for marker in _NEGOTIATION_MARKERS)
    has_order_marker = any(marker in folded for marker in _ORDER_MARKERS)
    inferred_money = explicit_money
    if (
        inferred_money is None
        and untyped
        and (has_negotiation_marker or has_order_marker or negotiation_active)
    ):
        inferred_money = _plain_number(untyped[-1])

    questions: list[str] = []
    if any(marker in folded for marker in _PRICE_QUESTION_MARKERS):
        questions.append("price")
    if ranges or any(marker in folded for marker in ("续航", "跑多远")):
        questions.append("range")
    if powers or any(marker in folded for marker in ("带得动", "能带")):
        questions.append("compatibility")

    ambiguities: list[str] = []
    needs_model = False
    if inferred_money is None and untyped:
        meaningful = [value for value in untyped if Decimal(value) >= Decimal(20)]
        if meaningful:
            ambiguities.append("存在没有单位且语义未确定的数字：" + "、".join(meaningful))
            needs_model = True

    is_negotiation = inferred_money is not None and (
        explicit_money is not None
        or has_negotiation_marker
        or has_order_marker
        or negotiation_active
    )
    is_order_request = any(marker in folded for marker in _ORDER_MARKERS)
    if is_negotiation:
        primary_intent = "price_negotiation"
    elif "price" in questions:
        primary_intent = "price_inquiry"
    elif ranges and any(marker in folded for marker in ("都能", "才", "这么少", "只有")):
        primary_intent = "range_comparison"
    elif "compatibility" in questions:
        primary_intent = "compatibility"
    elif "range" in questions:
        primary_intent = "range_inquiry"
    else:
        primary_intent = "unknown"

    quantity = int(quantities[-1].group(1)) if quantities else 1
    return CustomerSemantics(
        primary_intent=primary_intent,
        battery_model=battery_model,
        motor_power_w=_plain_number(powers[-1].group(1)) if powers else None,
        required_range_km=_plain_number(ranges[-1].group(1)) if ranges else None,
        quantity=quantity,
        money_offer=inferred_money,
        is_negotiation=is_negotiation,
        is_order_request=is_order_request,
        is_correction=any(marker in folded for marker in _CORRECTION_MARKERS),
        questions=tuple(dict.fromkeys(questions)),
        ambiguities=tuple(ambiguities),
        confidence=0.65 if needs_model else 1.0,
        evidence=tuple(evidence),
        needs_model_resolution=needs_model,
    )


def semantic_system_prompt() -> str:
    """Return the strict semantic-extraction instruction for ambiguous turns."""
    return (
        "你是客服消息语义解析器，只分析顾客整轮消息，不生成客服回复。"
        "必须区分金额、续航公里、功率W、电压V、容量Ah、数量、尺寸和电池型号。"
        "除非有金额单位、明确议价表达，或输入说明当前已处于议价状态且顾客回复裸数字，"
        "否则 money_offer 必须为 null。只返回JSON对象，不得返回Markdown。"
    )


def semantic_user_prompt(
    text: str,
    *,
    negotiation_active: bool,
    deterministic: CustomerSemantics,
) -> str:
    return json.dumps(
        {
            "customer_turn": text,
            "negotiation_active": negotiation_active,
            "deterministic_unit_evidence": [
                {"text": item, "type": kind} for item, kind in deterministic.evidence
            ],
            "output_contract": {
                "primary_intent": "string",
                "money_offer": "string|null",
                "is_negotiation": "boolean",
                "ambiguities": "string[]",
                "confidence": "number between 0 and 1",
                "evidence": "array of {text,type}",
            },
        },
        ensure_ascii=False,
    )


def parse_and_guard_model_semantics(
    raw_text: str,
    *,
    customer_text: str,
    deterministic: CustomerSemantics,
    negotiation_active: bool,
) -> CustomerSemantics:
    """Parse model JSON and apply the local unit/price authority boundary."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ValueError("语义模型未返回有效JSON。") from error
    if not isinstance(payload, dict):
        raise TypeError("语义模型结果必须是JSON对象。")
    model_offer = payload.get("money_offer")
    if model_offer is not None:
        model_offer = _plain_number(str(model_offer))
    model_negotiation = payload.get("is_negotiation") is True
    confidence_raw = payload.get("confidence", 0.0)
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise TypeError("语义模型confidence字段无效。")
    confidence = max(0.0, min(float(confidence_raw), 1.0))

    # Explicit local evidence always wins.  In particular, a number already
    # bound to km/W/V/Ah/model/quantity can never be promoted to money.
    typed_values = {
        _plain_number(match.group(1))
        for pattern in (_RANGE_RE, _POWER_RE, _VOLTAGE_RE, _CAPACITY_RE)
        for match in pattern.finditer(normalize_for_matching(customer_text))
    }
    typed_values.update(
        _plain_number(match.group(1))
        for match in _COMPACT_BATTERY_RE.finditer(normalize_for_matching(customer_text))
    )
    if model_offer in typed_values:
        model_offer = None
        model_negotiation = False
        confidence = min(confidence, 0.5)

    folded = normalize_for_matching(customer_text)
    has_price_evidence = (
        bool(_MONEY_UNIT_RE.search(folded))
        or any(marker in folded for marker in _NEGOTIATION_MARKERS)
        or any(marker in folded for marker in _ORDER_MARKERS)
        or negotiation_active
    )
    if not has_price_evidence:
        model_offer = None
        model_negotiation = False

    ambiguities_raw = payload.get("ambiguities", [])
    ambiguities = (
        tuple(str(value) for value in ambiguities_raw if str(value).strip())
        if isinstance(ambiguities_raw, list)
        else deterministic.ambiguities
    )
    return replace(
        deterministic,
        primary_intent=str(payload.get("primary_intent") or deterministic.primary_intent),
        money_offer=model_offer,
        is_negotiation=model_negotiation and model_offer is not None,
        ambiguities=ambiguities,
        confidence=confidence,
        needs_model_resolution=False,
    )


def _plain_number(value: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("数字字段格式无效。") from error
    fixed = format(number, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed

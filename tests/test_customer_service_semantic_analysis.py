from __future__ import annotations

import json

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    NEGOTIATION_OPENING_COUNTERS,
)
from xianyu_assistant.customer_service.negotiation import build_negotiation_plan
from xianyu_assistant.customer_service.semantic_analysis import (
    analyze_customer_turn,
    parse_and_guard_model_semantics,
)


def _product(key: str):
    return next(product for product in CURRENT_PRODUCTS if product.product_key == key)


def test_range_comparison_numbers_are_not_money() -> None:
    semantics = analyze_customer_turn("我4824都能50Km")

    assert semantics.battery_model == "48V24Ah"
    assert semantics.required_range_km == "50"
    assert semantics.money_offer is None
    assert semantics.is_negotiation is False
    assert semantics.primary_intent == "range_comparison"

    product = _product("current-tieta-60v30ah")
    assert (
        build_negotiation_plan(
            "我4824都能50Km",
            product,
            quantity=1,
            opening_counter=NEGOTIATION_OPENING_COUNTERS[product.product_key],
            previous=None,
            semantics=semantics,
        )
        is None
    )


def test_range_requirement_in_price_question_is_not_an_offer() -> None:
    semantics = analyze_customer_turn("60伏20安，25公里的多少钱")

    assert semantics.battery_model == "60V20Ah"
    assert semantics.required_range_km == "25"
    assert semantics.money_offer is None
    assert semantics.primary_intent == "price_inquiry"

    product = _product("current-tieta-60v20ah")
    assert (
        build_negotiation_plan(
            "60伏20安，25公里的多少钱",
            product,
            quantity=1,
            opening_counter=NEGOTIATION_OPENING_COUNTERS[product.product_key],
            previous=None,
            semantics=semantics,
        )
        is None
    )


def test_explicit_offer_and_active_bare_followup_are_money() -> None:
    explicit = analyze_customer_turn("350可以吗")
    followup = analyze_customer_turn("378", negotiation_active=True)

    assert explicit.money_offer == "350"
    assert explicit.is_negotiation is True
    assert followup.money_offer == "378"
    assert followup.is_negotiation is True


def test_model_cannot_promote_unit_bound_number_to_money() -> None:
    deterministic = analyze_customer_turn("350W带得动吗")
    guarded = parse_and_guard_model_semantics(
        json.dumps(
            {
                "primary_intent": "price_negotiation",
                "money_offer": "350",
                "is_negotiation": True,
                "confidence": 0.99,
                "ambiguities": [],
            }
        ),
        customer_text="350W带得动吗",
        deterministic=deterministic,
        negotiation_active=False,
    )

    assert guarded.motor_power_w == "350"
    assert guarded.money_offer is None
    assert guarded.is_negotiation is False

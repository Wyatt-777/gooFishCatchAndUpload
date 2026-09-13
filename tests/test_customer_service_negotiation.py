from __future__ import annotations

import pytest

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    NEGOTIATION_OPENING_COUNTERS,
)
from xianyu_assistant.customer_service.models import ReplyProposal
from xianyu_assistant.customer_service.negotiation import (
    build_negotiation_plan,
    quantity_from_messages,
    validate_negotiation_reply,
)


def _product(model: str):
    return next(item for item in CURRENT_PRODUCTS if model in item.aliases)


def _plan(query: str, model: str, *, previous=None, quantity: int = 1):
    product = _product(model)
    plan = build_negotiation_plan(
        query,
        product,
        quantity=quantity,
        opening_counter=NEGOTIATION_OPENING_COUNTERS[product.product_key],
        previous=previous,
    )
    assert plan is not None
    return plan


@pytest.mark.parametrize(
    ("model", "query", "price"),
    (
        ("6020", "380能卖吗", "380"),
        ("6030", "650可以吗", "650"),
        ("4830", "500能出吗", "500"),
    ),
)
def test_offer_inside_approved_range_is_accepted(model: str, query: str, price: str) -> None:
    plan = _plan(query, model)

    assert plan.outcome == "accept"
    assert plan.accepted_price == price
    assert plan.fallback_reply == f"{price}可以"


def test_voltage_and_capacity_are_never_parsed_as_a_customer_offer() -> None:
    product = _product("4830")

    assert (
        build_negotiation_plan(
            "48伏30安怎么卖",
            product,
            quantity=1,
            opening_counter=NEGOTIATION_OPENING_COUNTERS[product.product_key],
            previous=None,
        )
        is None
    )


def test_first_below_floor_offer_uses_approved_opening_counter() -> None:
    plan = _plan("350行不行", "6020")

    assert plan.outcome == "reject"
    assert plan.reply_price == "388"
    assert plan.fallback_reply == "350不行 388可以"


def test_improved_second_offer_moves_to_floor_but_does_not_accept_below_it() -> None:
    first = _plan("350行不行", "6020")
    second = _plan("那370呢", "6020", previous=first.next_state)

    assert second.outcome == "reject"
    assert second.reply_price == "378"
    assert second.fallback_reply == "370不行 378可以"


def test_repeated_low_offer_changes_wording_without_changing_counter() -> None:
    first = _plan("628行不行", "6030")
    second = _plan("别人都是628", "6030", previous=first.next_state)
    third = _plan("我已经拍了 给我改628", "6030", previous=second.next_state)

    assert first.reply_price == second.reply_price == third.reply_price == "668"
    assert first.fallback_reply == "628不行 668可以"
    assert second.fallback_reply == "这个价做不了 668可以"
    assert third.fallback_reply == "拍了也不行 只能按668改"
    assert len({first.fallback_reply, second.fallback_reply, third.fallback_reply}) == 3
    assert third.next_state.repeated_offer_count == 2


def test_two_unit_explicit_total_offer_uses_total_floor_and_counter() -> None:
    plan = _plan("两组750可以吗", "6020", quantity=2)

    assert plan.price_basis == "total"
    assert plan.outcome == "reject"
    assert plan.reply_price == "776"


def test_two_unit_normal_price_is_calculated_by_local_rules() -> None:
    plan = _plan("两组多少钱", "6020", quantity=2)

    assert plan.outcome == "quote"
    assert plan.reply_price == "796"
    assert plan.fallback_reply == "2组796 包邮"


def test_bare_followup_offer_after_two_units_requires_unit_or_total_clarification() -> None:
    assert quantity_from_messages("750可以吗", ("我要两组6020",)) == 2
    plan = _plan("750可以吗", "6020", quantity=2)

    assert plan.outcome == "clarify"
    assert "总价" in plan.fallback_reply


def test_customer_can_clarify_previous_multi_item_offer_as_total() -> None:
    ambiguous = _plan("750可以吗", "6020", quantity=2)
    clarified = _plan(
        "是两组总价",
        "6020",
        quantity=2,
        previous=ambiguous.next_state,
    )

    assert clarified.price_basis == "total"
    assert clarified.outcome == "reject"
    assert clarified.customer_offer == "750"
    assert clarified.reply_price == "776"


def test_multi_item_order_price_change_requires_handoff() -> None:
    plan = _plan("两组750拍下了 改价", "6020", quantity=2)

    assert plan.requires_handoff is True


def test_conditional_purchase_keeps_previously_accepted_price() -> None:
    accepted = _plan("650可以吗", "6030")
    followup = _plan("可以我就拍", "6030", previous=accepted.next_state)

    assert followup.outcome == "accept"
    assert followup.accepted_price == "650"


def test_plain_order_request_does_not_trigger_an_unasked_discount() -> None:
    product = _product("6020")

    assert (
        build_negotiation_plan(
            "那就要这个 怎么下单",
            product,
            quantity=1,
            opening_counter=NEGOTIATION_OPENING_COUNTERS[product.product_key],
            previous=None,
        )
        is None
    )


def test_validator_rejects_model_that_changes_local_outcome() -> None:
    plan = _plan("650可以吗", "6030")

    with pytest.raises(ValueError, match="表示拒绝"):
        validate_negotiation_reply(ReplyProposal(reply_text="650不行 只能668"), plan)
    with pytest.raises(ValueError, match="最低"):
        validate_negotiation_reply(ReplyProposal(reply_text="最低650可以"), plan)


def test_validator_rejects_exact_recent_negotiation_reply() -> None:
    plan = _plan("628行不行", "6030")

    with pytest.raises(ValueError, match="完全重复"):
        validate_negotiation_reply(
            ReplyProposal(reply_text="628不行 668 行的话直接拍"),
            plan,
            recent_merchant_replies=("628不行，668行的话直接拍",),
        )

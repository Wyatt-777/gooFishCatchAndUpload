"""Saved product prices are the authority for customer-facing quotes."""

from dataclasses import replace
from pathlib import Path

import pytest

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    FIRST_CONTACT_MESSAGE_SETTING,
    PRICE_SILENCE_FOLLOW_UP_SETTING,
    catalog_message_prices_match,
    configured_catalog_reply,
    configured_multi_model_price_reply,
    install_current_catalog,
)
from xianyu_assistant.customer_service.customer_service_worker import (
    _configured_first_contact_message,
    _configured_price_silence_follow_up,
    _validate_semantic_reply,
)
from xianyu_assistant.customer_service.models import ReplyProposal
from xianyu_assistant.customer_service.negotiation import NegotiationState, build_negotiation_plan
from xianyu_assistant.customer_service.semantic_analysis import analyze_customer_turn
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def test_edited_prices_drive_catalog_and_multi_model_quote(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    install_current_catalog(repository)
    product = repository.get_product_knowledge("current-tieta-60v30ah")
    assert product is not None
    repository.save_product_knowledge(replace(product, listed_price="699.00"))

    catalog = configured_catalog_reply(repository)
    assert "60V30A 699元" in catalog
    assert "60V30A 678元" not in catalog
    assert "60V20A 398元" in catalog
    assert configured_multi_model_price_reply(repository, "6030和6020分别多少钱") == (
        "原装25年铁塔 60V30Ah（6030） 699元，原装25年铁塔 60V20Ah（6020） 398元"
    )


def test_stale_or_swapped_fixed_prices_fall_back_to_saved_catalog(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    install_current_catalog(repository)
    product = repository.get_product_knowledge("current-tieta-60v30ah")
    assert product is not None
    repository.save_product_knowledge(replace(product, listed_price="699.00"))

    assert not catalog_message_prices_match(repository, "6030 678元")
    assert not catalog_message_prices_match(repository, "6030 398元")
    assert catalog_message_prices_match(repository, "6030 699元，蓝牙加20元")
    repository.set_setting(FIRST_CONTACT_MESSAGE_SETTING, "6030 678元")
    assert "60V30A 699元" in _configured_first_contact_message(repository)
    repository.set_setting(PRICE_SILENCE_FOLLOW_UP_SETTING, "6030 678元，想好了可以下单")
    assert _configured_price_silence_follow_up(repository) == ""


def test_plain_price_answer_cannot_use_old_curated_price() -> None:
    product = replace(CURRENT_PRODUCTS[0], listed_price="699.00")
    with pytest.raises(ValueError, match="当前商品标准价格"):
        _validate_semantic_reply(
            ReplyProposal(reply_text="6030 678元", intent="product_price"),
            analyze_customer_turn("6030多少钱"),
            knowledge=product,
            knowledge_answers=(),
            negotiation_plan=None,
        )
    with pytest.raises(ValueError, match="客服设置以外的报价"):
        _validate_semantic_reply(
            ReplyProposal(reply_text="29可以", intent="fit"),
            analyze_customer_turn("这个能装吗"),
            knowledge=product,
            knowledge_answers=(),
            negotiation_plan=None,
        )


def test_old_accepted_price_is_not_reused_after_minimum_price_increases() -> None:
    product = replace(CURRENT_PRODUCTS[1], minimum_price="390.00")
    previous = NegotiationState(
        product.product_key,
        quantity=1,
        last_customer_offer="380",
        last_counter="388",
        accepted_price="380",
        outcome="accept",
    )
    plan = build_negotiation_plan(
        "现在怎么拍",
        product,
        quantity=1,
        opening_counter="388.00",
        previous=previous,
    )
    assert plan is not None
    assert plan.accepted_price is None
    assert plan.reply_price == "390"

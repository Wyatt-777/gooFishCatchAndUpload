"""Regression tests for moderate proactive selling and safety exclusions."""

from xianyu_assistant.customer_service.models import (
    ChatMessage,
    MessageDirection,
    MessageKind,
    ProductKnowledge,
    ReplyProposal,
    SalesStage,
)
from xianyu_assistant.customer_service.sales import build_sales_plan


def _incoming(text: str) -> tuple[ChatMessage, ...]:
    return (ChatMessage("m1", MessageDirection.INCOMING, MessageKind.TEXT, text),)


def _product() -> ProductKnowledge:
    return ProductKnowledge(
        product_key="p1",
        name="60V20Ah",
        listed_price="398",
        minimum_price="378",
        specifications="二轮原装车单人平路续航约40公里",
    )


def test_moderate_sales_answers_then_asks_one_fit_question() -> None:
    proposal = ReplyProposal("398元", intent="price")

    plan = build_sales_plan(
        "6020多少钱",
        proposal,
        _product(),
        _incoming("6020多少钱"),
        negotiation_active=False,
    )

    assert plan.stage is SalesStage.QUALIFY
    assert plan.proposal.reply_text == "398元\n你是二轮还是三轮？我再帮你核下适配和续航"
    assert plan.follow_up_text == "方便说下车型和想跑的公里数，我再帮你核下适配"


def test_sales_is_paused_for_after_sales_and_negotiation() -> None:
    proposal = ReplyProposal("我确认下", intent="unknown")

    after_sales = build_sales_plan(
        "电池坏了要退货",
        proposal,
        _product(),
        _incoming("电池坏了要退货"),
        negotiation_active=False,
    )
    negotiation = build_sales_plan(
        "378可以吗",
        proposal,
        _product(),
        _incoming("378可以吗"),
        negotiation_active=True,
    )
    warranty = build_sales_plan(
        "质保多久",
        ReplyProposal("所有电池质保一年 容量虚标包退", intent="warranty"),
        _product(),
        _incoming("质保多久"),
        negotiation_active=False,
    )

    assert after_sales.stage is SalesStage.PAUSED
    assert after_sales.follow_up_text is None
    assert negotiation.stage is SalesStage.PAUSED
    assert negotiation.follow_up_text is None
    assert warranty.stage is SalesStage.PAUSED
    assert warranty.follow_up_text is None


def test_catalog_is_already_a_sales_move_and_is_not_lengthened() -> None:
    proposal = ReplyProposal("当前在售型号清单", intent="battery_catalog")

    plan = build_sales_plan(
        "你好",
        proposal,
        None,
        _incoming("你好"),
        negotiation_active=False,
    )

    assert plan.proposal.reply_text == proposal.reply_text
    assert plan.stage is SalesStage.RECOMMEND
    assert "二轮还是三轮" in (plan.follow_up_text or "")


def test_recent_identical_nudge_is_not_repeated() -> None:
    nudge = "你是二轮还是三轮？我再帮你核下适配和续航"
    plan = build_sales_plan(
        "多少钱",
        ReplyProposal("398元", intent="price"),
        _product(),
        _incoming("多少钱"),
        negotiation_active=False,
        recent_merchant_replies=(nudge,),
    )

    assert plan.proposal.reply_text == "398元"


def test_charger_compatibility_does_not_append_generic_qualification() -> None:
    messages = (
        ChatMessage("m1", MessageDirection.INCOMING, MessageKind.TEXT, "二轮的"),
        ChatMessage("m2", MessageDirection.INCOMING, MessageKind.TEXT, "送的匹配是吧"),
    )

    plan = build_sales_plan(
        "送的匹配是吧",
        ReplyProposal("对 送的配你原车口", intent="unknown"),
        None,
        messages,
        negotiation_active=False,
    )

    assert plan.proposal.reply_text == "对 送的配你原车口"
    assert plan.stage is SalesStage.PAUSED
    assert plan.follow_up_text is None


def test_shipping_confirmation_does_not_restart_product_qualification() -> None:
    plan = build_sales_plan(
        "好的",
        ReplyProposal("好的 准备给你发货", intent="unknown"),
        _product(),
        _incoming("好的"),
        negotiation_active=False,
    )

    assert plan.proposal.reply_text == "好的 准备给你发货"
    assert plan.stage is SalesStage.PAUSED
    assert plan.follow_up_text is None


def test_specific_product_fact_question_is_answered_without_generic_sales_question() -> None:
    plan = build_sales_plan(
        "6030尺寸多大",
        ReplyProposal("6030尺寸17-18-32", intent="unknown"),
        _product(),
        _incoming("6030尺寸多大"),
        negotiation_active=False,
    )

    assert plan.proposal.reply_text == "6030尺寸17-18-32"
    assert plan.stage is SalesStage.PAUSED
    assert plan.follow_up_text is None


def test_qualification_is_not_restarted_after_both_questions_were_already_asked() -> None:
    plan = build_sales_plan(
        "多少钱",
        ReplyProposal("398元", intent="price"),
        _product(),
        _incoming("多少钱"),
        negotiation_active=False,
        recent_merchant_replies=("你是二轮还是三轮？平时想跑多少公里",),
    )

    assert plan.proposal.reply_text == "398元"
    assert plan.stage is SalesStage.PAUSED
    assert plan.follow_up_text is None

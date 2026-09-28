"""Regression tests for moderate proactive selling and safety exclusions."""

from xianyu_assistant.customer_service.models import (
    ChatMessage,
    MessageDirection,
    MessageKind,
    ProductKnowledge,
    ReplyProposal,
    SalesStage,
)
from xianyu_assistant.customer_service.sales import (
    build_checkout_guidance_plan,
    build_sales_plan,
)


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


def test_price_only_answer_does_not_ask_about_vehicle_or_schedule_follow_up() -> None:
    proposal = ReplyProposal("398元", intent="price")

    plan = build_sales_plan(
        "6020多少钱",
        proposal,
        _product(),
        _incoming("6020多少钱"),
        negotiation_active=False,
    )

    assert plan.stage is SalesStage.PAUSED
    assert plan.proposal.reply_text == "398元"
    assert plan.follow_up_text is None


def test_customer_fit_question_may_ask_for_vehicle_without_old_stock_phrase() -> None:
    plan = build_sales_plan(
        "6020价格多少，适配我的车吗",
        ReplyProposal("398元，适配要看车型", intent="price"),
        _product(),
        _incoming("6020价格多少，适配我的车吗"),
        negotiation_active=False,
    )

    assert "你平时用什么车型" in plan.proposal.reply_text
    assert "二轮还是三轮" not in plan.proposal.reply_text


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
    assert plan.follow_up_text is None


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


def test_optional_guidance_keeps_price_and_catalog_replies_short() -> None:
    for query, proposal in (
        ("6020多少钱", ReplyProposal("6020 398元", intent="price")),
        ("实价吗", ReplyProposal("已保存的型号价格话术", intent="battery_catalog")),
    ):
        plan = build_checkout_guidance_plan(
            query,
            proposal,
            _product(),
            _incoming(query),
            negotiation_active=False,
            selected_model="60V20Ah" if "6020" in query else None,
            quantity=1,
        )
        assert plan.proposal == proposal
        assert plan.follow_up_text is None


def test_optional_guidance_asks_one_missing_model_for_purchase_intent() -> None:
    plan = build_checkout_guidance_plan(
        "我想买一组",
        ReplyProposal("有的", intent="unknown"),
        _product(),
        _incoming("我想买一组"),
        negotiation_active=False,
        selected_model=None,
        quantity=1,
    )

    assert plan.proposal.reply_text == "有的\n你要哪个型号"
    assert plan.follow_up_text == "型号发我下 我帮你确认"
    assert plan.stage is SalesStage.QUALIFY


def test_optional_guidance_checks_fit_before_checkout() -> None:
    plan = build_checkout_guidance_plan(
        "能装就要",
        ReplyProposal("我帮你看看", intent="unknown"),
        _product(),
        _incoming("能装就要"),
        negotiation_active=False,
        selected_model="60V20Ah",
        quantity=1,
    )

    assert plan.proposal.reply_text.endswith("电池仓尺寸发我下 我帮你核")
    assert "链接拍" not in plan.proposal.reply_text
    assert plan.follow_up_text == "电池仓尺寸方便发我下吗"


def test_optional_guidance_only_prompts_checkout_for_confirmed_single_product() -> None:
    ready = build_checkout_guidance_plan(
        "怎么拍",
        ReplyProposal("可以", intent="unknown"),
        _product(),
        _incoming("怎么拍"),
        negotiation_active=False,
        selected_model="60V20Ah",
        quantity=1,
    )
    wrong_model = build_checkout_guidance_plan(
        "怎么拍",
        ReplyProposal("可以", intent="unknown"),
        _product(),
        _incoming("怎么拍"),
        negotiation_active=False,
        selected_model="60V30Ah",
        quantity=1,
    )
    multiple = build_checkout_guidance_plan(
        "怎么拍",
        ReplyProposal("可以", intent="unknown"),
        _product(),
        _incoming("怎么拍"),
        negotiation_active=False,
        selected_model="60V20Ah",
        quantity=2,
    )

    assert ready.proposal.reply_text == "可以\n在这个链接拍就行"
    assert ready.stage is SalesStage.CLOSE
    assert ready.follow_up_text is None
    assert wrong_model.proposal.reply_text == "可以"
    assert multiple.proposal.reply_text == "可以"


def test_optional_guidance_does_not_push_after_decline_or_model_only_answer() -> None:
    for query in ("再看看", "不要一组了", "60 30"):
        proposal = ReplyProposal("好的", intent="unknown")
        plan = build_checkout_guidance_plan(
            query,
            proposal,
            _product(),
            _incoming(query),
            negotiation_active=False,
            selected_model="60V20Ah" if query == "60 30" else None,
            quantity=1,
        )
        assert plan.proposal == proposal
        assert plan.follow_up_text is None

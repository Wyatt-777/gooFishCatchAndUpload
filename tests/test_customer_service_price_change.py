from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_assistant.customer_service.models import (
    PriceChangeDraft,
    PriceChangeStatus,
    ProductKnowledge,
)
from xianyu_assistant.customer_service.price_change import (
    PricePolicyError,
    check_price_change,
    parse_money,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def _product(*, minimum: str | None = "400", listed: str | None = "428") -> ProductKnowledge:
    return ProductKnowledge(
        product_key="battery-6020",
        name="60V20Ah 铁塔电池",
        listed_price=listed,
        minimum_price=minimum,
    )


def test_money_uses_exact_decimal_and_rejects_model_number_as_price() -> None:
    assert str(parse_money("￥428.10")) == "428.10"
    with pytest.raises(PricePolicyError):
        parse_money("6020 428")
    with pytest.raises(PricePolicyError):
        parse_money("428.001")


def test_price_change_enforces_explicit_catalog_floor() -> None:
    decision = check_price_change("400", _product())
    assert decision.approved_price == "400.00"
    assert decision.minimum_price == "400.00"

    with pytest.raises(PricePolicyError, match="低于最低成交价"):
        check_price_change("399.99", _product())
    with pytest.raises(PricePolicyError, match="未设置最低成交价"):
        check_price_change("428", _product(minimum=None))
    with pytest.raises(PricePolicyError, match="未设置标准成交价"):
        check_price_change("410", _product(listed=None))
    with pytest.raises(PricePolicyError, match="高于标准成交价"):
        check_price_change("428.01", _product())


def test_repository_persists_price_change_audit(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    draft = PriceChangeDraft(
        task_id="pc-1",
        conversation_key="session-1",
        customer_message_key="message-9",
        product_key="battery-6020",
        product_name="60V20Ah 铁塔电池",
        customer_offer="410",
        proposed_price="410.00",
        minimum_price="400.00",
        listed_price="428.00",
        rationale="已确认型号与成交价",
    )

    repository.save_price_change_draft(draft)
    stored = repository.list_price_change_drafts()

    assert stored == [draft]
    repository.save_price_change_draft(
        PriceChangeDraft(
            **{
                **{field: getattr(draft, field) for field in draft.__dataclass_fields__},
                "status": PriceChangeStatus.APPLIED,
            }
        )
    )
    assert repository.list_price_change_drafts()[0].status is PriceChangeStatus.APPLIED


def test_price_change_adjustment_metadata_must_be_complete() -> None:
    with pytest.raises(ValueError, match="加价项名称和金额必须同时存在"):
        PriceChangeDraft(
            task_id="pc-incomplete-option",
            conversation_key="session-1",
            customer_message_key="message-9",
            product_key="battery-6020",
            product_name="60V20Ah 铁塔电池",
            proposed_price="400.00",
            minimum_price="398.00",
            listed_price="418.00",
            price_adjustment_key="bluetooth_module",
        )

from __future__ import annotations

from pathlib import Path

from xianyu_assistant.customer_service.current_catalog import (
    BLUETOOTH_UPGRADE_AMOUNT,
    CURRENT_CATALOG_REPLY,
    install_current_catalog,
)
from xianyu_assistant.customer_service.models import HistoricalExample, ProductKnowledge
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def test_current_catalog_archives_old_rows_and_installs_three_authoritative_products(
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    repository.save_product_knowledge(
        ProductKnowledge(
            product_key="xianyu-old-battery",
            name="旧电池商品",
            aliases=("旧型号",),
            listed_price="428",
            enabled=True,
        )
    )
    repository.save_curated_example(
        HistoricalExample(
            "old-6020-price",
            "6020多少钱",
            "60V20Ah 428元 健康度100",
            "user_curated",
        )
    )
    repository.save_curated_example(
        HistoricalExample(
            "current-business-bluetooth-20260913",
            "可以加装蓝牙吗 加装多少钱",
            "可以 加装蓝牙补40元",
            "user_curated",
        )
    )
    repository.save_curated_example(
        HistoricalExample(
            "curated-battery-catalog-bluetooth-price",
            "加装蓝牙模块多少钱",
            "补40元差价",
            "user_curated",
        )
    )

    assert install_current_catalog(repository) is True

    enabled = repository.list_product_knowledge(include_disabled=False)
    assert {product.product_key for product in enabled} == {
        "current-tieta-60v30ah",
        "current-tieta-60v20ah",
        "current-tieta-48v30ah",
    }
    assert {product.listed_price for product in enabled} == {"678.00", "398.00", "518.00"}
    assert {product.minimum_price for product in enabled} == {
        "650.00",
        "378.00",
        "480.00",
    }
    assert repository.find_product_knowledge_for_query("6030多少钱").listed_price == "678.00"
    assert repository.find_product_knowledge_for_query("问下48伏30安续航").listed_price == "518.00"
    assert repository.find_product_knowledge_for_query("有什么电池") is None
    assert any(
        example.merchant_text == CURRENT_CATALOG_REPLY
        and example.trust_level == "user_curated"
        for example in repository.list_historical_examples()
    )
    assert "蓝牙模块可选装 加20元" in CURRENT_CATALOG_REPLY
    assert all(
        "428元" not in example.merchant_text
        for example in repository.find_knowledge_answers("6020多少钱", 10)
    )
    single_product_answers = repository.find_knowledge_answers("4830多少钱", 10)
    assert any("48V30A 518元" in item.merchant_text for item in single_product_answers)
    assert all("60V20A 398元" not in item.merchant_text for item in single_product_answers)
    multi_product_answers = repository.find_knowledge_answers(
        "6020和6030分别多少钱",
        10,
    )
    assert any("60V20A 398元" in item.merchant_text for item in multi_product_answers)
    assert any("60V30A 678元" in item.merchant_text for item in multi_product_answers)
    carrier_answers = repository.find_knowledge_answers("发什么快递", 10)
    assert any(
        item.merchant_text == "默认发安能物流和京东"
        for item in carrier_answers
    )
    bluetooth_answers = repository.find_knowledge_answers("加装蓝牙多少钱", 10)
    assert BLUETOOTH_UPGRADE_AMOUNT == "20.00"
    assert any(
        item.merchant_text == "可以 蓝牙自己选装 加装补20元"
        for item in bluetooth_answers
    )
    assert all("40元" not in item.merchant_text for item in bluetooth_answers)
    superseded_bluetooth = next(
        item
        for item in repository.list_historical_examples()
        if item.example_id == "curated-battery-catalog-bluetooth-price"
    )
    assert superseded_bluetooth.trust_level == "superseded_catalog"
    assert all(
        "选装时在商品价格基础上增加20元" in product.supplementary_knowledge
        and "40元" not in product.supplementary_knowledge
        for product in enabled
    )
    assert install_current_catalog(repository) is False


def test_catalog_version_does_not_overwrite_later_operator_edit(tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    install_current_catalog(repository)
    product = repository.get_product_knowledge("current-tieta-60v20ah")
    assert product is not None
    repository.save_product_knowledge(
        ProductKnowledge(
            product_key=product.product_key,
            name=product.name,
            aliases=product.aliases,
            listed_price="398.00",
            minimum_price="380.00",
            specifications=product.specifications,
            inventory_notes=product.inventory_notes,
            supplementary_knowledge=product.supplementary_knowledge,
        )
    )

    assert install_current_catalog(repository) is False
    assert repository.get_product_knowledge(product.product_key).minimum_price == "380.00"

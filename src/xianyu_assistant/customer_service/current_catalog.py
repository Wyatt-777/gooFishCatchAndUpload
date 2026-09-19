"""User-confirmed current battery catalog, superseding historical chat facts."""

from __future__ import annotations

from typing import Protocol

from xianyu_assistant.customer_service.models import HistoricalExample, ProductKnowledge


class CatalogRepository(Protocol):
    def get_setting(self, name: str, default: str | None = None) -> str | None: ...

    def set_setting(self, name: str, value: str) -> None: ...

    def archive_products_except(self, current_product_keys: tuple[str, ...]) -> int: ...

    def save_product_knowledge(self, product: ProductKnowledge) -> None: ...

    def save_curated_example(self, example: HistoricalExample) -> None: ...

    def archive_conflicting_catalog_examples(
        self, current_example_ids: tuple[str, ...]
    ) -> int: ...

CATALOG_VERSION = "iron-tower-2026-09-20-v6-6030-range"

BLUETOOTH_UPGRADE_AMOUNT = "20.00"

NEGOTIATION_OPENING_COUNTERS = {
    "current-tieta-60v20ah": "388.00",
    "current-tieta-60v30ah": "668.00",
    "current-tieta-48v30ah": "508.00",
}

CURRENT_OPERATING_POLICY = {
    "shipping": "默认包邮；新疆、内蒙古、西藏不包邮；海南不发货",
    "shipping_origin": "广东普宁",
    "default_carriers": "安能物流和京东",
    "bluetooth_upgrade": "蓝牙模块由顾客选装，选装时在商品价格基础上增加20元",
    "charger": "默认赠送充电器",
    "capacity_video": "可提供发货前容量测试视频，顾客咨询时转人工处理",
    "warranty": "所有电池质保一年，容量虚标包退",
    "first_use": "电池到货后先充满电，再装车使用",
    "charging_duration": "正常充电约5-6小时",
}

CURRENT_PRODUCTS = (
    ProductKnowledge(
        product_key="current-tieta-60v30ah",
        name="原装25年铁塔 60V30Ah（6030）",
        aliases=("6030", "60V30A", "60V30Ah", "60伏30安", "铁塔6030"),
        listed_price="678.00",
        minimum_price="650.00",
        specifications=(
            "型号：60V30Ah（6030）\n"
            "尺寸：17-18-32\n"
            "实测容量：29Ah左右\n"
            "健康度：97%以上\n"
            "二轮原装车单人平路续航预估：50-60公里左右"
        ),
        inventory_notes="当前在售",
        shipping_notes="默认包邮；新疆、内蒙古、西藏不包邮；海南不发货；广东普宁发货；默认发安能物流和京东。",
        after_sales_notes="所有电池质保一年，容量虚标包退。",
        supplementary_knowledge=(
            "原装25年铁塔。续航为二轮原装车、单人、平路条件下的预估。"
            "蓝牙模块由顾客选装，选装时在商品价格基础上增加20元。"
            "默认赠送充电器。可提供发货前容量测试视频，顾客咨询时转人工处理。"
            "电池到货后先充满电，再装车使用。正常充电约5-6小时。"
        ),
    ),
    ProductKnowledge(
        product_key="current-tieta-60v20ah",
        name="原装25年铁塔 60V20Ah（6020）",
        aliases=("6020", "60V20A", "60V20Ah", "60伏20安", "铁塔6020"),
        listed_price="398.00",
        minimum_price="378.00",
        specifications=(
            "型号：60V20Ah（6020）\n"
            "尺寸：14.5-17-29\n"
            "实测容量：19Ah左右\n"
            "健康度：97%以上\n"
            "二轮原装车单人平路续航预估：40公里左右"
        ),
        inventory_notes="当前在售",
        shipping_notes="默认包邮；新疆、内蒙古、西藏不包邮；海南不发货；广东普宁发货；默认发安能物流和京东。",
        after_sales_notes="所有电池质保一年，容量虚标包退。",
        supplementary_knowledge=(
            "原装25年铁塔。续航为二轮原装车、单人、平路条件下的预估。"
            "蓝牙模块由顾客选装，选装时在商品价格基础上增加20元。"
            "默认赠送充电器。可提供发货前容量测试视频，顾客咨询时转人工处理。"
            "电池到货后先充满电，再装车使用。正常充电约5-6小时。"
        ),
    ),
    ProductKnowledge(
        product_key="current-tieta-48v30ah",
        name="原装25年铁塔 48V30Ah（4830）",
        aliases=("4830", "48V30A", "48V30Ah", "48伏30安", "铁塔4830"),
        listed_price="518.00",
        minimum_price="480.00",
        specifications=(
            "型号：48V30Ah（4830）\n"
            "尺寸：15-17.5-30\n"
            "实测容量：29Ah左右\n"
            "健康度：97%以上\n"
            "二轮原装车单人平路续航预估：50公里左右"
        ),
        inventory_notes="当前在售",
        shipping_notes="默认包邮；新疆、内蒙古、西藏不包邮；海南不发货；广东普宁发货；默认发安能物流和京东。",
        after_sales_notes="所有电池质保一年，容量虚标包退。",
        supplementary_knowledge=(
            "原装25年铁塔。续航为二轮原装车、单人、平路条件下的预估。"
            "蓝牙模块由顾客选装，选装时在商品价格基础上增加20元。"
            "默认赠送充电器。可提供发货前容量测试视频，顾客咨询时转人工处理。"
            "电池到货后先充满电，再装车使用。正常充电约5-6小时。"
        ),
    ),
)

FIRST_CONTACT_CATALOG_REPLY = """原装25年铁塔
60V30A 678元 尺寸17-18-32
容量29安左右 健康度97以上
二轮原装车单人平路续航预估50-60公里左右
60V20A 398元 尺寸14.5-17-29
容量19安左右 健康度97以上
二轮原装车单人平路续航预估40公里左右
48V30A 518元 尺寸15-17.5-30
容量29安左右 健康度97以上
二轮原装车单人平路续航预估50公里左右
蓝牙模块可选装 加20元"""

CURRENT_CATALOG_REPLY = FIRST_CONTACT_CATALOG_REPLY + """
默认送充电器
默认包邮 新疆内蒙古西藏除外 海南不发货
所有电池质保一年 容量虚标包退"""

CURRENT_QA_EXAMPLES = (
    HistoricalExample(
        "current-catalog-overview-20260913",
        "目前有哪些在售电池 型号价格尺寸容量健康度续航",
        CURRENT_CATALOG_REPLY,
        "user_curated",
    ),
    HistoricalExample(
        "current-tieta-6030-20260913",
        "6030 60V30A多少钱 尺寸多大 容量健康度和续航",
        "60V30A 678元 尺寸17-18-32 容量29安左右 健康度97以上 二轮原装车单人平路续航预估50-60公里左右",
        "user_curated",
    ),
    HistoricalExample(
        "current-tieta-6020-20260913",
        "6020 60V20A多少钱 尺寸多大 容量健康度和续航",
        "60V20A 398元 尺寸14.5-17-29 容量19安左右 健康度97以上 二轮原装车单人平路续航预估40公里左右",
        "user_curated",
    ),
    HistoricalExample(
        "current-tieta-4830-20260913",
        "4830 48V30A多少钱 尺寸多大 容量健康度和续航",
        "48V30A 518元 尺寸15-17.5-30 容量29安左右 健康度97以上 二轮原装车单人平路续航预估50公里左右",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-shipping-20260913",
        "包邮吗 从哪里发",
        "包邮 广东普宁发",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-carriers-20260914",
        "默认发什么快递 哪家物流",
        "默认发安能物流和京东",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-bluetooth-20260913",
        "可以加装蓝牙吗 加装多少钱",
        "可以 蓝牙自己选装 加装补20元",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-first-charge-20260913",
        "电池刚收到要先充电吗 充多久",
        "到货先充满电再装车 正常充电约5-6小时",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-sales-policy-20260919",
        "送充电器吗 包邮吗 哪些地区不发 质保多久 容量虚标怎么办",
        "默认送充电器 默认包邮 新疆内蒙古西藏不包邮 海南不发货 所有电池质保一年 容量虚标包退",
        "user_curated",
    ),
    HistoricalExample(
        "current-business-capacity-video-20260919",
        "能发容量测试视频吗 发货前看容量视频",
        "可以提供 这个转人工处理",
        "user_curated",
    ),
)


def install_current_catalog(repository: CatalogRepository) -> bool:
    """Install this catalog once without overwriting later operator edits."""
    keys = tuple(product.product_key for product in CURRENT_PRODUCTS)
    # Imports and live-history sync may recreate historical product rows. Keep
    # those records for audit, but never allow them back into the current sale list.
    repository.archive_products_except(keys)
    repository.archive_conflicting_catalog_examples(
        tuple(example.example_id for example in CURRENT_QA_EXAMPLES)
    )
    if repository.get_setting("current_product_catalog_version") == CATALOG_VERSION:
        return False
    for product in CURRENT_PRODUCTS:
        repository.save_product_knowledge(product)
    for example in CURRENT_QA_EXAMPLES:
        repository.save_curated_example(example)
    repository.set_setting("current_product_catalog_version", CATALOG_VERSION)
    return True

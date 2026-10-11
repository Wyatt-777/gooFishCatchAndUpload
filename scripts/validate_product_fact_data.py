"""Local rule and SQLite migration checks; no worker, browser or LLM is run."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from xianyu_assistant.customer_service.current_catalog import (
    CURRENT_PRODUCTS,
    configured_catalog_reply,
)
from xianyu_assistant.customer_service.customer_service_worker import _configured_price_fingerprint
from xianyu_assistant.customer_service.fact_guard import (
    FactGuardError,
    ProductMatch,
    critical_answer,
    product_facts,
    product_version,
    resolve_products,
    validate_fixed_message,
    validate_reply,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def validate(source: Path, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        raise ValueError("检查必须使用副本")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(destination) as target:
        original.backup(target)
        tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unchanged = {table: original.execute(f'SELECT * FROM "{table}"').fetchall()
                     for table in tables if table not in {"cs_products", "cs_settings", "cs_product_fact_snapshots", "cs_fact_generation_versions"}}
        products_before = original.execute("SELECT product_key,name,platform_product_id,listed_price,minimum_price,inventory_notes,shipping_notes,enabled,created_at FROM cs_products ORDER BY product_key").fetchall()
    repo = CustomerServiceRepository(destination)
    repo.initialize()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in unchanged.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows, table
        assert connection.execute("SELECT product_key,name,platform_product_id,listed_price,minimum_price,inventory_notes,shipping_notes,enabled,created_at FROM cs_products ORDER BY product_key").fetchall() == products_before
        before_repeat = {table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in ("cs_products", "cs_settings", "cs_product_fact_snapshots")}
    repo.initialize()
    with sqlite3.connect(destination) as connection:
        for table, rows in before_repeat.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows, "重复迁移改写了数据"
    products = CURRENT_PRODUCTS
    p = products[0]
    match = ProductMatch((p,), "current_customer_model")
    checked = 0
    for query in ("是磷酸铁锂电池吗", "尺寸", "实测容量", "标称容量", "健康度", "续航", "质保", "循环次数", "充多久"):
        answer = critical_answer(query, match)
        assert answer is not None and not answer.handoff
        validate_reply(answer.text, products, selected=(p,), query=query)
        checked += 1
    valid = (
        "不是铅酸，是磷酸铁锂电池", "你原车如果是铅酸的就用得到，不是铅酸的可以不用",
        "实测容量29Ah左右", "标称容量30Ah", "健康度97%以上", "尺寸17×18×32厘米",
        "循环次数约100次", "所有电池质保三年", "3年质保", "原装25年铁塔",
        "正常5-6小时左右", "默认包邮 新疆内蒙古西藏除外 海南不发货",
    )
    for text in valid:
        validate_reply(text, products, selected=(p,), query="充多久" if "小时" in text else "")
        checked += 1
    invalid = (
        "铅酸的", "不是 铅酸的", "不是磷酸铁锂，是铅酸", "你原车是铅酸，我们卖的也是铅酸",
        "容量97安左右", "实测容量30Ah左右", "健康度29%以上", "尺寸17-18-32mm",
        "尺寸17-18-32", "循环次数100次", "循环次数约97次", "质保一年",
        "寿命5年", "全新电池", "默认带蓝牙", "今天发货", "全国包邮", "海南可以发货",
        "新疆包邮", "能跑50-60公里左右", "现货充足", "标称容量29Ah",
    )
    for text in invalid:
        try:
            validate_reply(text, products, selected=(p,))
        except FactGuardError:
            pass
        else:
            raise AssertionError("应拦截但未拦截：" + text)
        checked += 1
    assert resolve_products(products, "60 30", ("6020",), platform_product_id="unbound").products == (p,)
    assert resolve_products(products, "容量是多少", ("6030", "6020")).products == (products[1],)
    assert len(resolve_products(products, "6030和6020").products) == 2
    assert not resolve_products(products, "72V30Ah", ("6030",)).products
    variant = replace(p, product_key="another-6030", name="另一个6030商品")
    assert not resolve_products((*products, variant), "6030").products
    assert not resolve_products(products, "6030元").products
    assert product_facts(replace(p, supplementary_knowledge="不是磷酸铁锂")).conflicts
    assert product_facts(replace(p, supplementary_knowledge="铅酸电池")).conflicts
    assert critical_answer("寿命几年", match).handoff
    assert critical_answer("电池类型", ProductMatch((), "unresolved")).clarification
    assert product_version(p) != product_version(replace(p, inventory_notes="有货"))
    checked += 11
    welcome = configured_catalog_reply(repo)
    validate_fixed_message(repo, welcome)
    validate_fixed_message(repo, configured_catalog_reply(repo, include_business=True))
    follow = repo.get_setting("price_silence_follow_up_message", "")
    if follow:
        validate_fixed_message(repo, follow)
    first = repo.list_product_knowledge(include_disabled=False)[0]
    version = _configured_price_fingerprint(repo)
    repo.save_product_knowledge(replace(first, inventory_notes=first.inventory_notes + "\n检查副本记录"))
    assert _configured_price_fingerprint(repo) != version
    repo.save_product_knowledge(first)
    assert _configured_price_fingerprint(repo) == version
    return {"status": "passed", "rule_checks": checked, "unchanged_tables": len(unchanged),
            "database_integrity": "ok", "idempotent_migration": True, "price_preserved": True,
            "catalog_guard": "passed", "source": str(source), "copy": str(destination),
            "validation_scope": "本地规则与数据库副本，无聊天模拟、无浏览器、无LLM调用"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.source, args.copy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    args.copy.with_suffix(".validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

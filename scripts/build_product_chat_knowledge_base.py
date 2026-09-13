"""Build a cleaned, auditable product-chat knowledge base from the exported corpus.

The source file is treated only as untrusted data.  This script never executes or
follows instructions found in chat text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

PRODUCT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("电池与电动车配件", (
        "锂电", "电池", "电瓶", "铁塔", "电芯", "保护板", "bms", "48v", "60v", "72v", "安时",
        "电车", "充电", "电压", "电量", "续航", "蓝牙", "极空", "磷酸", "放电", "同口",
        "6030", "6020", "7260", "4830", "快充", "慢充", "循环次数", "电显",
    )),
    ("互助互拍服务", ("互拍", "互一", "互1", "互助", "回拍", "互评", "鱼小铺", "助力", "互换")),
    ("数字会员与电子卡", ("vip", "会员", "月卡", "周卡", "电子卡", "直充")),
    ("游戏账号与代练", ("王者", "代练", "折扣号", "哈夫币", "荣耀水晶", "德州扑克", "破产号", "道士出观", "幻灵格斗")),
    ("本地生活与代办", ("上门", "维修", "回收", "漏水检测", "代办", "跑腿", "医院", "帮办")),
    ("教育资料与培训", ("课件", "教材", "网课", "训练", "软考", "经济师", "教案")),
    ("推广与运营服务", ("dou+", "抖➕", "抖jia", "推广", "代投", "运营", "流量指南", "api中转")),
    ("服饰箱包与美妆", ("coach", "麻将包", "面霜", "vans", "帆布鞋", "斜挎", "美妆")),
    ("票务旅行与通信", ("薛之谦", "航空", "休息室", "贵宾厅", "流量卡", "移动联通电信")),
    ("家居建材与家电", ("地铺石", "石英砖", "地砖", "电视", "空调", "冰箱", "洗衣机")),
    ("宠物与其他实物", ("仓鼠", "主粮", "娃娃", "套件")),
    ("医疗美容咨询", ("隆胸", "医美", "假体", "自体脂肪")),
)
TARGET_PRODUCT_CATEGORY = "电池与电动车配件"

INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("价格与议价", ("多少钱", "价格", "多钱", "标价", "改价", "便宜", "优惠", "元", "几块")),
    ("库存与购买", ("有货", "库存", "还有吗", "怎么买", "怎么拍", "下单", "要几", "几组", "现货")),
    ("规格与配置", ("型号", "尺寸", "容量", "安时", "多少安", "多少a", "多少v", "电芯", "保护板", "蓝牙", "密码", "app", "配置")),
    ("适配与安装", ("可以用", "能用", "适配", "安装", "怎么接", "接线", "装车", "空开", "二轮", "三轮")),
    ("性能与续航", ("跑多远", "续航", "能跑", "电量", "健康度", "测试", "测容", "足容", "电压")),
    ("充电与使用", ("充电", "充满", "充多久", "充电器", "变绿灯", "怎么用", "使用")),
    ("质量与安全", ("质量", "坏", "断电", "发热", "膨", "爆炸", "起火", "危险", "锁了", "故障", "问题")),
    ("物流与发货", ("发货", "快递", "物流", "单号", "包邮", "运费", "哪里发", "多久到", "收到")),
    ("退换与售后", ("退货", "退款", "退换", "售后", "取回", "客服介入", "投诉", "报销")),
    ("线下地点与联系", ("在哪里", "哪里呢", "地址", "上门", "电话", "联系", "同城", "过来看看")),
    ("交易流程", ("付款", "拍下", "专拍", "交易", "确认收货", "评价")),
    ("互拍互评", ("互拍", "回拍", "互评", "小红花", "助力", "互一元", "互1元")),
)

SYSTEM_MARKERS = (
    "我已拍下", "我已付款", "我已发货", "我已修改价格", "我已为你设置专拍价",
    "等待你付款", "等待你发货", "等待你处理", "去处理", "去付款", "去发货",
    "收到小红花", "我完成了评价", "交易成功", "交易关闭", "申请客服介入",
    "退货退款申请将超时", "请双方沟通及时确认价格", "请包装好商品",
    "此消息不支持在pc端浏览", "前往手机闲鱼app", "记得及时发货",
    "记得及时确认收货", "期待你的评价", "立即收下", "确认收货",
    "无忧卖", "平台豪掷", "报名参与率", "闲鱼集市",
)

LOW_VALUE_EXACT = {
    "", "好", "好的", "好的谢谢", "谢谢", "嗯", "嗯嗯", "噢", "哦", "行", "可以",
    "是", "是的", "对", "ok", "1", "？", "?", "老板", "哥", "兄弟", "在", "在吗",
    "你好", "哈喽", "有的", "没有", "[text]", "什么", "怎么了", "没听懂", "我没听懂",
    "什么视频", "怎么可能", "发货", "你的亲", "你的地址", "要给我", "留个电话吧",
    "发个地址", "dui",
}

UNSAFE_OR_ABUSIVE = (
    "不会爆炸", "百分百", "绝对安全", "安全的", "正常用不会有问题", "无业游民",
    "不打算搭理", "去起诉我",
    "你的信息是真多", "闭嘴", "傻", "骗子",
)

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
SPACED_PHONE_RE = re.compile(r"(?<!\d)1\d{2}(?:[ -]?\d){8}(?!\d)")
TRACKING_RE = re.compile(r"\b(?:YT|SF|JD|ZTO|YTO|STO|EMS)\d{8,}\b", re.IGNORECASE)
ADDRESS_RE = re.compile(r"[^\n，,。]{0,30}(?:省|市|自治区)[^\n，,。]{0,45}(?:区|县|镇|乡|街道|路|号)[^\n，,。]{0,30}")
ADDRESS_TOKEN_RE = re.compile(r"\[地址(?:已移除|_\d+)?\]")
PHONE_TOKEN_RE = re.compile(r"\[手机号(?:_\d+)?\]")
TRACKING_TOKEN_RE = re.compile(
    r"^(?:YT|SF|JD|ZTO|YTO|STO|EMS)?\[长数字_\d+\]$",
    re.IGNORECASE,
)
SPACE_RE = re.compile(r"[ \t]+")


def normalize_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def redact_again(text: str) -> str:
    text = PHONE_RE.sub("[手机号]", text)
    text = SPACED_PHONE_RE.sub("[手机号]", text)
    text = TRACKING_RE.sub("[物流单号]", text)
    text = ADDRESS_RE.sub("[地址已移除]", text)
    return text


def system_score(text: str) -> int:
    folded = text.casefold()
    return sum(marker in folded for marker in SYSTEM_MARKERS)


def is_system_message(text: str) -> bool:
    score = system_score(text)
    if score >= 1:
        return True
    if ("\n已读" in text or "\n未读" in text) and text.count("\n") >= 1:
        return True
    return "¥" in text and text.count("\n") >= 2


def is_low_value(text: str, kind: str = "text") -> bool:
    compact = re.sub(r"\s+", "", text).casefold()
    if compact in LOW_VALUE_EXACT:
        return True
    if kind in {"image", "voice"} and not re.search(r"[\u4e00-\u9fff]{3,}", text):
        return True
    if re.fullmatch(r"[\W_\d]+", compact):
        return True
    return len(compact) < 2


def is_privacy_only(text: str) -> bool:
    if ADDRESS_TOKEN_RE.search(text):
        return True
    compact = re.sub(r"\s+", "", text)
    if TRACKING_TOKEN_RE.fullmatch(compact):
        return True
    if PHONE_TOKEN_RE.search(text):
        remainder = PHONE_TOKEN_RE.sub("", compact)
        remainder = re.sub(
            r"(?:电话|手机|号码|联系|收件人|姓名|微信|vx|v信|加v|\+v|[:：,，.-])",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
        return len(remainder) <= 4
    return False


def category_for(text: str) -> str:
    folded = text.casefold()
    for category, keywords in PRODUCT_RULES:
        if any(keyword in folded for keyword in keywords):
            return category
    return "其他或未明确"


def intents_for(text: str) -> list[str]:
    folded = text.casefold()
    intents = [name for name, keywords in INTENT_RULES if any(word in folded for word in keywords)]
    return intents or ["一般商品咨询"]


def content_hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def pick_product_key(conversations: Iterable[dict[str, Any]]) -> tuple[str | None, bool]:
    keys = [str(c["product_key"]) for c in conversations if c.get("product_key")]
    if not keys:
        return None, False
    counts = Counter(keys)
    return counts.most_common(1)[0][0], len(counts) > 1


def clean_message(message: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    text = redact_again(normalize_text(message.get("text")))
    kind = str(message.get("message_type") or "text")
    if not text:
        return None, "empty"
    if is_system_message(text):
        return None, "system_card"
    if any(marker in text.casefold() for marker in UNSAFE_OR_ABUSIVE):
        return None, "unsafe_or_abusive"
    if is_privacy_only(text):
        return None, "privacy_only"
    if is_low_value(text, kind):
        return None, "low_value"
    return {
        "message_key": message.get("message_key"),
        "direction": message.get("direction"),
        "message_type": kind,
        "text": text,
        "platform_time": message.get("platform_time"),
    }, None


def build(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    raw = json.loads(source.read_text(encoding="utf-8"))
    products = raw.get("products", [])
    conversations = raw.get("conversations", [])
    examples = raw.get("historical_examples", [])
    product_by_key = {str(p["product_key"]): p for p in products}

    occurrence = Counter(
        str(message.get("message_key") or "")
        for conversation in conversations
        for message in conversation.get("messages", [])
        if message.get("message_key")
    )
    anomalous_keys = {
        key for key, count in occurrence.items()
        if count >= max(20, len(conversations) // 10)
    }

    exclusion_counts: Counter[str] = Counter()
    staged: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for conversation in conversations:
        cleaned_messages: list[dict[str, Any]] = []
        seen_in_conversation: set[str] = set()
        for message in conversation.get("messages", []):
            key = str(message.get("message_key") or "")
            if key and key in anomalous_keys:
                exclusion_counts["cross_conversation_contamination"] += 1
                continue
            if key and key in seen_in_conversation:
                exclusion_counts["duplicate_within_conversation"] += 1
                continue
            if key:
                seen_in_conversation.add(key)
            cleaned, reason = clean_message(message)
            if cleaned is None:
                exclusion_counts[reason or "other"] += 1
            else:
                cleaned_messages.append(cleaned)
        if not cleaned_messages:
            exclusion_counts["conversation_empty_after_cleaning"] += 1
            continue
        staged.append((conversation, cleaned_messages))

    # Exact message-key sequence duplicates are alternate captures of the same chat.
    groups: dict[tuple[str, ...], list[tuple[dict[str, Any], list[dict[str, Any]]]]] = defaultdict(list)
    for conversation, messages in staged:
        signature = tuple(str(m.get("message_key") or content_hash(m["text"])) for m in messages)
        groups[signature].append((conversation, messages))

    chat_docs: list[dict[str, Any]] = []
    product_chat_counts: Counter[str] = Counter()
    product_intents: dict[str, Counter[str]] = defaultdict(Counter)
    for signature, group in groups.items():
        source_conversations = [entry[0] for entry in group]
        messages = max((entry[1] for entry in group), key=len)
        product_key, link_conflict = pick_product_key(source_conversations)
        product = product_by_key.get(product_key or "")
        combined = "\n".join(message["text"] for message in messages)
        title = normalize_text(product.get("name")) if product else ""
        title_category = category_for(title)
        content_category = category_for(combined)
        product_category = content_category if content_category != "其他或未明确" else title_category
        intents = intents_for(combined)
        direction_unreliable = len(messages) >= 4 and len({m.get("direction") for m in messages}) == 1
        source_keys = sorted({str(c["conversation_key"]) for c in source_conversations})
        doc_id = "chat-" + content_hash(*signature)[:20]
        quality_flags: list[str] = []
        if link_conflict:
            quality_flags.append("product_link_conflict")
        if (
            title_category != "其他或未明确"
            and content_category != "其他或未明确"
            and title_category != content_category
        ):
            quality_flags.append("product_category_mismatch")
        if not product_key:
            quality_flags.append("product_unlinked")
        if direction_unreliable:
            quality_flags.append("speaker_direction_may_be_unreliable")
        if any(m["message_type"] != "text" for m in messages):
            quality_flags.append("contains_untranscribed_media")
        chat_docs.append({
            "knowledge_id": doc_id,
            "document_type": "product_chat",
            "product_key": product_key,
            "platform_product_id": product.get("platform_product_id") if product else None,
            "product_name": title or None,
            "product_category": product_category,
            "chat_intents": intents,
            "messages": messages,
            "content": "\n".join(f"{m.get('direction') or 'unknown'}: {m['text']}" for m in messages),
            "source_conversation_keys": source_keys,
            "quality_flags": quality_flags,
            "usage_note": "历史事实与表达样本；涉及价格、库存、安全、兼容性和售后时必须重新确认。",
        })
        if product_key:
            product_chat_counts[product_key] += 1
            for intent in intents:
                product_intents[product_key][intent] += 1

    faq_docs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    faq_exclusions: Counter[str] = Counter()
    for example in examples:
        customer = redact_again(normalize_text(example.get("customer_text")))
        merchant = redact_again(normalize_text(example.get("merchant_text")))
        pair = (customer, merchant)
        if pair in seen_pairs:
            faq_exclusions["duplicate"] += 1
            continue
        seen_pairs.add(pair)
        if not customer or not merchant or is_low_value(customer) or is_low_value(merchant):
            faq_exclusions["low_value"] += 1
            continue
        if is_privacy_only(customer) or is_privacy_only(merchant):
            faq_exclusions["privacy_only"] += 1
            continue
        if is_system_message(customer) or is_system_message(merchant) or system_score(customer) + system_score(merchant) >= 2:
            faq_exclusions["system_card"] += 1
            continue
        folded = (customer + "\n" + merchant).casefold()
        if any(marker in folded for marker in UNSAFE_OR_ABUSIVE):
            faq_exclusions["unsafe_or_abusive"] += 1
            continue
        category = category_for(folded)
        intents = intents_for(folded)
        faq_docs.append({
            "knowledge_id": "faq-" + content_hash(customer, merchant)[:20],
            "document_type": "historical_qa_candidate",
            "product_category": category,
            "chat_intents": intents,
            "customer_text": customer,
            "merchant_text": merchant,
            "content": f"顾客：{customer}\n商家：{merchant}",
            "trust_level": "historical_unverified",
            "quality_flags": ["product_unlinked", "facts_require_verification"],
            "usage_note": "仅作检索与口吻参考，不得直接作为价格、库存、安全承诺或售后政策。",
        })

    # This project is battery-focused.  Keep only clearly battery-related
    # products and documents so unrelated marketplace history cannot enter
    # product matching or wording retrieval.
    kept_product_keys = {
        str(product["product_key"])
        for product in products
        if category_for(normalize_text(product.get("name"))) == TARGET_PRODUCT_CATEGORY
    }
    removed_product_count = len(products) - len(kept_product_keys)
    removed_chat_count = sum(
        document["product_category"] != TARGET_PRODUCT_CATEGORY for document in chat_docs
    )
    removed_faq_count = sum(
        document["product_category"] != TARGET_PRODUCT_CATEGORY for document in faq_docs
    )
    exclusion_counts["non_battery_category"] += removed_chat_count
    faq_exclusions["non_battery_category"] += removed_faq_count
    chat_docs = [
        document
        for document in chat_docs
        if document["product_category"] == TARGET_PRODUCT_CATEGORY
    ]
    faq_docs = [
        document
        for document in faq_docs
        if document["product_category"] == TARGET_PRODUCT_CATEGORY
    ]
    for document in chat_docs:
        if document.get("product_key") not in kept_product_keys:
            document["product_key"] = None
            document["platform_product_id"] = None
            document["product_name"] = None
            if "product_unlinked" not in document["quality_flags"]:
                document["quality_flags"].append("product_unlinked")

    product_chat_counts = Counter()
    product_intents = defaultdict(Counter)
    for document in chat_docs:
        product_key = document.get("product_key")
        if product_key:
            product_chat_counts[product_key] += 1
            for intent in document["chat_intents"]:
                product_intents[product_key][intent] += 1

    cleaned_products: list[dict[str, Any]] = []
    for product in products:
        key = str(product["product_key"])
        name = normalize_text(product.get("name"))
        if key not in kept_product_keys:
            continue
        aliases = sorted({normalize_text(a) for a in product.get("aliases", []) if normalize_text(a)})
        cleaned_products.append({
            "product_key": key,
            "platform_product_id": product.get("platform_product_id"),
            "name": name,
            "aliases": aliases,
            "product_category": category_for(name),
            "listed_price": product.get("listed_price"),
            "minimum_price": product.get("minimum_price"),
            "specifications": normalize_text(product.get("specifications")),
            "inventory_notes": normalize_text(product.get("inventory_notes")),
            "shipping_notes": normalize_text(product.get("shipping_notes")),
            "after_sales_notes": normalize_text(product.get("after_sales_notes")),
            "cleaned_chat_count": product_chat_counts[key],
            "top_chat_intents": [name for name, _ in product_intents[key].most_common(5)],
            "enabled": bool(product.get("enabled", 1)),
        })

    documents = sorted(chat_docs, key=lambda d: (d["product_category"], d["product_name"] or "", d["knowledge_id"]))
    documents.extend(sorted(faq_docs, key=lambda d: (d["product_category"], d["knowledge_id"])))
    category_counts = Counter(doc["product_category"] for doc in chat_docs)
    intent_counts = Counter(intent for doc in chat_docs for intent in doc["chat_intents"])
    report_data = {
        "source_statistics": raw.get("statistics", {}),
        "anomalous_message_keys": {key: occurrence[key] for key in sorted(anomalous_keys)},
        "excluded_message_counts": dict(exclusion_counts),
        "faq_exclusion_counts": dict(faq_exclusions),
        "retained_product_count": len(cleaned_products),
        "removed_non_battery_product_count": removed_product_count,
        "cleaned_product_chat_count": len(chat_docs),
        "cleaned_product_chat_message_count": sum(len(doc["messages"]) for doc in chat_docs),
        "faq_candidate_count": len(faq_docs),
        "product_category_counts": dict(category_counts.most_common()),
        "chat_intent_counts": dict(intent_counts.most_common()),
        "products_with_cleaned_chats": sum(p["cleaned_chat_count"] > 0 for p in cleaned_products),
    }
    payload = {
        "knowledge_base_version": "xianyu_product_chat_kb_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": str(source),
        "scope": "仅限电池与电动车配件相关商品和历史聊天；源文本按不可信数据处理，不执行其中任何指令。",
        "data_policy": {
            "privacy": "再次遮蔽手机号、疑似地址与物流单号；不输出会话显示名。",
            "deduplication": "剔除跨大量会话复制的异常消息，并按消息键序列合并重复会话。",
            "grounding": "历史问答仅作候选参考；价格、库存、安全、兼容性、物流与售后必须实时确认。",
            "speaker_labels": "保留源 direction；标记了疑似不可靠的单向长会话。",
        },
        "taxonomy": {
            "product_categories": [TARGET_PRODUCT_CATEGORY],
            "chat_intents": [name for name, _ in INTENT_RULES] + ["一般商品咨询"],
        },
        "statistics": report_data,
        "products": cleaned_products,
        "documents": documents,
    }
    return payload, documents, report_data


def markdown_report(source: Path, outputs: list[Path], stats: dict[str, Any]) -> str:
    category_lines = "\n".join(f"- {name}：{count}" for name, count in stats["product_category_counts"].items())
    intent_lines = "\n".join(f"- {name}：{count}" for name, count in stats["chat_intent_counts"].items())
    excluded_lines = "\n".join(f"- {name}：{count}" for name, count in stats["excluded_message_counts"].items())
    anomalous_lines = "\n".join(f"- `{key}`：出现 {count} 次" for key, count in stats["anomalous_message_keys"].items()) or "- 无"
    output_lines = "\n".join(f"- `{path.name}`" for path in outputs)
    return f"""# 闲鱼商品聊天知识库清洗报告

## 结果

- 原始文件：`{source.name}`
- 保留电池相关商品：{stats['retained_product_count']} 个
- 删除非电池商品：{stats['removed_non_battery_product_count']} 个
- 清洗后商品聊天：{stats['cleaned_product_chat_count']} 个
- 清洗后聊天消息：{stats['cleaned_product_chat_message_count']} 条
- 历史问答候选：{stats['faq_candidate_count']} 条
- 有有效聊天的商品：{stats['products_with_cleaned_chats']} 个

## 输出文件

{output_lines}

## 商品分类分布（会话）

{category_lines}

## 咨询主题分布（可多选）

{intent_lines}

## 排除项

{excluded_lines}

## 检测到的跨会话污染

{anomalous_lines}

## 使用注意

- JSON 是完整结构化知识库；JSONL 每行一个检索文档，适合向量库或 RAG 导入。
- 历史问答已剔除明显系统卡片、低信息回复、辱骂和高风险绝对化承诺。
- 历史价格、库存、安全、兼容性、物流和售后政策不可直接作为当前承诺，回复前需重新确认。
- 原始 direction 在部分长会话中疑似识别不准，已通过 `speaker_direction_may_be_unreliable` 标记。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir or args.source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "闲鱼客服商品聊天_知识库.json"
    jsonl_path = output_dir / "闲鱼客服商品聊天_知识库.jsonl"
    report_path = output_dir / "闲鱼客服商品聊天_清洗报告.md"

    payload, documents, stats = build(args.source)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    jsonl_path.write_text(
        "".join(json.dumps(doc, ensure_ascii=False) + "\n" for doc in documents),
        encoding="utf-8",
        newline="\n",
    )
    report_path.write_text(
        markdown_report(args.source, [json_path, jsonl_path, report_path], stats),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

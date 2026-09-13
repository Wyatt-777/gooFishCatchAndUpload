"""Build a searchable battery Q&A knowledge base from every trustworthy chat turn.

Chat text is untrusted input.  The builder only normalizes, classifies, and writes
data; it never executes instructions found in the corpus.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from scripts.build_product_chat_knowledge_base import (
        TARGET_PRODUCT_CATEGORY,
        UNSAFE_OR_ABUSIVE,
        category_for,
        content_hash,
        intents_for,
        is_privacy_only,
        is_system_message,
        normalize_text,
        redact_again,
    )
except ModuleNotFoundError:  # Support direct execution from the scripts directory.
    from build_product_chat_knowledge_base import (  # type: ignore[no-redef]
        TARGET_PRODUCT_CATEGORY,
        UNSAFE_OR_ABUSIVE,
        category_for,
        content_hash,
        intents_for,
        is_privacy_only,
        is_system_message,
        normalize_text,
        redact_again,
    )

SHORT_MERCHANT_ANSWERS = {
    "可以",
    "可以的",
    "有",
    "有的",
    "没有",
    "没了",
    "是",
    "是的",
    "对",
    "对的",
    "行",
    "包邮",
    "不包",
    "稍等",
    "马上",
}
EXTRA_UNSAFE_MARKERS = (
    "真烦人",
    "苍蝇",
    "滚",
    "去死",
    "有病",
    "废话",
    "不想搭理",
)
MEDIA_ONLY_RE = re.compile(r"^(?:[\w*]+\n)?(?:图片|语音|视频|\[image\]|\[voice\])$", re.IGNORECASE)
PRICE_RE = re.compile(r"(?:[¥￥]\s*\d|\d+(?:\.\d+)?\s*(?:元|块|一组|一套))")
DYNAMIC_FACT_MARKERS = (
    "价格",
    "多少钱",
    "多钱",
    "库存",
    "有货",
    "现货",
    "发货",
    "包邮",
    "运费",
    "退货",
    "退款",
    "售后",
    "保修",
    "安全",
    "爆炸",
    "起火",
)


def _clean_text(value: object) -> str:
    return redact_again(normalize_text(value))


def _is_unsafe(text: str) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in (*UNSAFE_OR_ABUSIVE, *EXTRA_UNSAFE_MARKERS))


def _usable_customer(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(
        compact
        and len(compact) >= 2
        and not is_system_message(text)
        and not is_privacy_only(text)
        and not _is_unsafe(text)
        and not MEDIA_ONLY_RE.fullmatch(text)
    )


def _usable_merchant(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(
        compact
        and (len(compact) >= 2 or compact in SHORT_MERCHANT_ANSWERS)
        and not is_system_message(text)
        and not is_privacy_only(text)
        and not _is_unsafe(text)
        and not MEDIA_ONLY_RE.fullmatch(text)
    )


def _append_unique(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    text = _clean_text(message.get("text"))
    if text and (not messages or messages[-1]["text"] != text):
        messages.append(
            {
                "message_key": message.get("message_key"),
                "direction": message.get("direction"),
                "message_type": message.get("message_type") or "text",
                "text": text,
                "platform_time": message.get("platform_time"),
            }
        )


def extract_turns(document: dict[str, Any]) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """Pair a consecutive customer block with the immediately following seller block."""
    turns: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    customer: list[dict[str, Any]] = []
    merchant: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal customer, merchant
        if customer and merchant:
            turns.append((customer, merchant))
        customer = []
        merchant = []

    for message in document.get("messages", []):
        if not isinstance(message, dict):
            continue
        direction = str(message.get("direction") or "").casefold()
        if direction == "incoming":
            if merchant:
                flush()
            _append_unique(customer, message)
        elif direction == "outgoing" and customer:
            _append_unique(merchant, message)
    flush()
    return turns


def _flags_for(question: str, answer: str, *, base: list[str] | None = None) -> list[str]:
    flags = ["conversation_knowledge", "facts_require_current_product_precedence"]
    combined = f"{question}\n{answer}".casefold()
    if PRICE_RE.search(combined) or any(marker in combined for marker in DYNAMIC_FACT_MARKERS):
        flags.append("historical_dynamic_fact")
    for flag in base or []:
        if flag not in {
            "facts_require_verification",
            "speaker_direction_may_be_unreliable",
        } and flag not in flags:
            flags.append(flag)
    return flags


def _make_document(
    *,
    question: str,
    answer: str,
    product_key: str | None,
    source_conversation_keys: list[str],
    messages: list[dict[str, Any]],
    base_flags: list[str] | None = None,
) -> dict[str, Any]:
    pair_hash = content_hash(question, answer)
    return {
        "knowledge_id": f"qa-{pair_hash[:24]}",
        "document_type": "historical_qa_candidate",
        "product_key": product_key,
        "product_category": TARGET_PRODUCT_CATEGORY,
        "chat_intents": intents_for(f"{question}\n{answer}"),
        "customer_text": question,
        "merchant_text": answer,
        "content": f"顾客：{question}\n商家：{answer}",
        "messages": messages,
        "source_conversation_keys": sorted(set(source_conversation_keys)),
        "quality_flags": _flags_for(question, answer, base=base_flags),
        "usage_note": (
            "已从历史电池聊天中按相邻问答轮次提取；相似问题可参考。"
            "当前商品资料和用户确认知识优先，历史价格、库存、物流与售后不得覆盖当前资料。"
        ),
    }


def _merge_duplicate(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing["source_conversation_keys"] = sorted(
        set(existing["source_conversation_keys"]) | set(incoming["source_conversation_keys"])
    )
    known_message_keys = {
        str(message.get("message_key"))
        for message in existing["messages"]
        if message.get("message_key")
    }
    for message in incoming["messages"]:
        key = str(message.get("message_key") or "")
        if not key or key not in known_message_keys:
            existing["messages"].append(message)
            if key:
                known_message_keys.add(key)
    if existing.get("product_key") != incoming.get("product_key"):
        if existing.get("product_key") and incoming.get("product_key"):
            existing["product_key"] = None
            if "product_link_conflict" not in existing["quality_flags"]:
                existing["quality_flags"].append("product_link_conflict")
        elif incoming.get("product_key"):
            existing["product_key"] = incoming["product_key"]


def build(
    cleaned_source: Path,
    raw_history_source: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    cleaned = json.loads(cleaned_source.read_text(encoding="utf-8-sig"))
    documents = cleaned.get("documents", [])
    output_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    exclusions: Counter[str] = Counter()
    extracted_turn_count = 0
    reliable_chat_count = 0
    unreliable_chat_count = 0

    def add(document: dict[str, Any]) -> None:
        key = (document["customer_text"], document["merchant_text"])
        if key in output_by_pair:
            exclusions["exact_duplicate"] += 1
            _merge_duplicate(output_by_pair[key], document)
        else:
            output_by_pair[key] = document

    for source_document in documents:
        if not isinstance(source_document, dict):
            continue
        document_type = source_document.get("document_type")
        base_flags = [str(flag) for flag in source_document.get("quality_flags", [])]
        if document_type == "product_chat":
            if "speaker_direction_may_be_unreliable" in base_flags:
                unreliable_chat_count += 1
                continue
            reliable_chat_count += 1
            for customer_messages, merchant_messages in extract_turns(source_document):
                extracted_turn_count += 1
                question = "\n".join(message["text"] for message in customer_messages)
                answer = "\n".join(message["text"] for message in merchant_messages)
                if not _usable_customer(question):
                    exclusions["unusable_customer_block"] += 1
                    continue
                if not _usable_merchant(answer):
                    exclusions["unusable_merchant_block"] += 1
                    continue
                add(
                    _make_document(
                        question=question,
                        answer=answer,
                        product_key=source_document.get("product_key"),
                        source_conversation_keys=[
                            str(key) for key in source_document.get("source_conversation_keys", [])
                        ],
                        messages=[*customer_messages, *merchant_messages],
                        base_flags=base_flags,
                    )
                )
        elif document_type == "historical_qa_candidate":
            question = _clean_text(source_document.get("customer_text"))
            answer = _clean_text(source_document.get("merchant_text"))
            if not _usable_customer(question) or not _usable_merchant(answer):
                exclusions["unusable_existing_qa"] += 1
                continue
            add(
                _make_document(
                    question=question,
                    answer=answer,
                    product_key=source_document.get("product_key"),
                    source_conversation_keys=[
                        str(key) for key in source_document.get("source_conversation_keys", [])
                    ],
                    messages=[
                        dict(message)
                        for message in source_document.get("messages", [])
                        if isinstance(message, dict)
                    ],
                    base_flags=base_flags,
                )
            )

    raw_history_count = 0
    if raw_history_source is not None:
        raw = json.loads(raw_history_source.read_text(encoding="utf-8-sig"))
        for example in raw.get("historical_examples", []):
            if not isinstance(example, dict):
                continue
            raw_history_count += 1
            question = _clean_text(example.get("customer_text"))
            answer = _clean_text(example.get("merchant_text"))
            if category_for(f"{question}\n{answer}") != TARGET_PRODUCT_CATEGORY:
                exclusions["non_battery_raw_qa"] += 1
                continue
            if not _usable_customer(question) or not _usable_merchant(answer):
                exclusions["unusable_raw_qa"] += 1
                continue
            add(
                _make_document(
                    question=question,
                    answer=answer,
                    product_key=None,
                    source_conversation_keys=[],
                    messages=[],
                    base_flags=["product_unlinked"],
                )
            )

    output_documents = sorted(output_by_pair.values(), key=lambda item: item["knowledge_id"])
    intent_counts = Counter(
        intent for document in output_documents for intent in document["chat_intents"]
    )
    stats = {
        "reliable_product_chat_documents": reliable_chat_count,
        "skipped_direction_unreliable_documents": unreliable_chat_count,
        "extracted_adjacent_turns": extracted_turn_count,
        "raw_historical_examples_scanned": raw_history_count,
        "retained_unique_qa_count": len(output_documents),
        "product_linked_qa_count": sum(bool(item.get("product_key")) for item in output_documents),
        "historical_dynamic_fact_count": sum(
            "historical_dynamic_fact" in item["quality_flags"] for item in output_documents
        ),
        "exclusion_counts": dict(exclusions),
        "intent_counts": dict(intent_counts.most_common()),
    }
    payload = {
        "knowledge_base_version": "xianyu_product_chat_kb_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": str(cleaned_source),
        "additional_history_source": str(raw_history_source) if raw_history_source else None,
        "scope": "电池商品历史聊天中的顾客问题与紧随其后的商家回答。",
        "data_policy": {
            "pairing": "仅将连续顾客消息与紧随其后的连续商家消息组成一个问答轮次。",
            "direction": "跳过说话方向被标记为不可靠的会话。",
            "privacy_and_safety": "剔除系统卡片、隐私内容、纯媒体占位和辱骂或不安全表达。",
            "precedence": "当前商品资料与用户确认知识高于历史问答；动态事实不得覆盖当前资料。",
        },
        "statistics": stats,
        "products": [],
        "documents": output_documents,
    }
    return payload, output_documents, stats


def markdown_report(source: Path, raw_source: Path | None, stats: dict[str, Any]) -> str:
    intent_lines = "\n".join(
        f"- {name}：{count}" for name, count in stats["intent_counts"].items()
    ) or "- 无"
    exclusion_lines = "\n".join(
        f"- {name}：{count}" for name, count in stats["exclusion_counts"].items()
    ) or "- 无"
    return f"""# 闲鱼客服全量问答知识库分析报告

## 数据范围

- 清洗会话知识库：`{source.name}`
- 补充历史问答：`{raw_source.name if raw_source else '未提供'}`
- 可靠电池聊天文档：{stats['reliable_product_chat_documents']} 个
- 跳过方向不可靠文档：{stats['skipped_direction_unreliable_documents']} 个
- 相邻问答轮次候选：{stats['extracted_adjacent_turns']} 个
- 扫描原有历史问答：{stats['raw_historical_examples_scanned']} 条

## 最终结果

- 去重后可检索问答：{stats['retained_unique_qa_count']} 条
- 保留商品关联的问答：{stats['product_linked_qa_count']} 条
- 含历史价格/库存/物流/售后等动态事实：{stats['historical_dynamic_fact_count']} 条
- 所有条目均标记为 `conversation_knowledge`，低于 `user_curated`，高于普通清洗历史。

## 问题分类

{intent_lines}

## 排除与去重

{exclusion_lines}

## 模型使用规则

- 相似问题可引用历史商家回答，并结合当前商品资料生成简短回复。
- 冲突时按“当前商品资料 / 用户确认知识 > 全量历史问答 > 普通清洗历史”处理。
- 历史价格、库存、安全、兼容性、物流与售后只作为旧记录，不得覆盖当前资料。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cleaned_source", type=Path)
    parser.add_argument("--raw-history-source", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir or args.cleaned_source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "闲鱼客服全量问答_知识库.json"
    jsonl_path = output_dir / "闲鱼客服全量问答_知识库.jsonl"
    report_path = output_dir / "闲鱼客服全量问答_分析报告.md"

    payload, documents, stats = build(args.cleaned_source, args.raw_history_source)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    jsonl_path.write_text(
        "".join(json.dumps(document, ensure_ascii=False) + "\n" for document in documents),
        encoding="utf-8",
        newline="\n",
    )
    report_path.write_text(
        markdown_report(args.cleaned_source, args.raw_history_source, stats),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

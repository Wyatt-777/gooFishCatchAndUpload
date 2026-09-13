"""Analyze seller voice and dialogue structure from reviewed battery chat turns.

Chat text is untrusted data.  The script reads it only as text, excludes unreliable
speaker direction, system/unsafe/privacy-only messages and long catalog templates,
then writes aggregate statistics without copying customer identifiers.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from scripts.build_full_chat_qa_knowledge_base import (
        _usable_customer,
        _usable_merchant,
        extract_turns,
    )
except ModuleNotFoundError:  # Support direct execution from the scripts directory.
    from build_full_chat_qa_knowledge_base import (  # type: ignore[no-redef]
        _usable_customer,
        _usable_merchant,
        extract_turns,
    )

CATALOG_PREFIX = "性价比原装"
FORMAL_SERVICE_PHRASES = ("您好", "亲", "感谢咨询", "很高兴为您服务")
REFERENCE_PHRASES = (
    "可以",
    "对",
    "行",
    "好的",
    "我看看",
    "发我看看",
    "稍等",
    "差不多",
    "正常",
    "你要哪个",
    "哥",
)
QUESTION_END_RE = re.compile(r"(?:哪个|什么|多少|吗|呢|咋|怎么|是不是|要不要|有没有)[？?]?$|[？?]")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF]")
TERMINAL_PUNCTUATION = "。！？?!"


def _compact_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _nearest_percentile(values: list[int], percentage: float) -> int:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentage)
    return ordered[index]


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def analyze(source: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    product_chats = [
        document
        for document in payload.get("documents", [])
        if document.get("document_type") == "product_chat"
    ]
    reliable_chats = [
        document
        for document in product_chats
        if "speaker_direction_may_be_unreliable" not in document.get("quality_flags", [])
    ]

    pairs: list[tuple[str, str]] = []
    first_pairs: list[tuple[str, str]] = []
    for document in reliable_chats:
        valid_pairs: list[tuple[str, str]] = []
        for customer_messages, merchant_messages in extract_turns(document):
            question = "\n".join(message["text"] for message in customer_messages)
            answer = "\n".join(message["text"] for message in merchant_messages)
            if _usable_customer(question) and _usable_merchant(answer):
                valid_pairs.append((question, answer))
        pairs.extend(valid_pairs)
        if valid_pairs:
            first_pairs.append(valid_pairs[0])

    conversational_pairs = [
        (question, answer)
        for question, answer in pairs
        if _compact_length(answer) <= 80 and not answer.startswith(CATALOG_PREFIX)
    ]
    answers = [answer for _, answer in conversational_pairs]
    lengths = [_compact_length(answer) for answer in answers]
    single_line_count = sum("\n" not in answer for answer in answers)
    terminal_punctuation_count = sum(
        bool(answer) and answer[-1] in TERMINAL_PUNCTUATION for answer in answers
    )
    clarification_count = sum(bool(QUESTION_END_RE.search(answer)) for answer in answers)
    formal_phrase_count = sum(
        any(phrase in answer for phrase in FORMAL_SERVICE_PHRASES) for answer in answers
    )
    emoji_or_exclamation_count = sum(
        bool(EMOJI_RE.search(answer)) or "!" in answer or "！" in answer for answer in answers
    )
    phrase_counts = {
        phrase: sum(phrase in answer for answer in answers) for phrase in REFERENCE_PHRASES
    }

    return {
        "analysis_version": "seller_chat_style_v1",
        "source_file": str(source),
        "scope": {
            "product_chat_documents": len(product_chats),
            "reliable_product_chat_documents": len(reliable_chats),
            "usable_adjacent_qa_turns": len(pairs),
            "first_reply_turns": len(first_pairs),
            "conversational_style_answers": len(answers),
            "excluded_catalog_or_long_answer_count": len(pairs) - len(answers),
        },
        "voice_metrics": {
            "compact_character_p25": _nearest_percentile(lengths, 0.25),
            "compact_character_median": _nearest_percentile(lengths, 0.5),
            "compact_character_p75": _nearest_percentile(lengths, 0.75),
            "compact_character_p90": _nearest_percentile(lengths, 0.9),
            "single_line_count": single_line_count,
            "single_line_ratio": _ratio(single_line_count, len(answers)),
            "terminal_punctuation_count": terminal_punctuation_count,
            "terminal_punctuation_ratio": _ratio(terminal_punctuation_count, len(answers)),
            "clarification_question_count": clarification_count,
            "clarification_question_ratio": _ratio(clarification_count, len(answers)),
            "formal_service_phrase_count": formal_phrase_count,
            "emoji_or_exclamation_count": emoji_or_exclamation_count,
            "reference_phrase_counts": phrase_counts,
        },
        "observed_dialogue_logic": [
            "先给当前问题的结论，不复述顾客原话。",
            "顾客补充型号或条件后，沿用上一轮问题直接回答。",
            "缺少关键条件时只追问一个信息点。",
            "确认类消息使用极短答复；多项事实用换行拆成短句。",
            "议价先给可接受结果或边界，技术问题先给一个动作再看结果。",
        ],
        "runtime_policy": [
            "风格只影响句式和回答顺序，不提供商品事实。",
            "当前商品资料优先于全部历史聊天，旧价格和旧规格不得复用。",
            "安全、售后和争议继续遵守转人工规则。",
        ],
    }


def markdown_report(analysis: dict[str, Any]) -> str:
    scope = analysis["scope"]
    metrics = analysis["voice_metrics"]
    phrase_lines = "\n".join(
        f"- {phrase}：{count} 次"
        for phrase, count in metrics["reference_phrase_counts"].items()
    )
    logic_lines = "\n".join(f"- {item}" for item in analysis["observed_dialogue_logic"])
    policy_lines = "\n".join(f"- {item}" for item in analysis["runtime_policy"])
    return f"""# 卖家聊天逻辑与语言风格分析

## 数据范围

- 电池商品聊天文档：{scope['product_chat_documents']} 个
- 说话方向可靠的聊天：{scope['reliable_product_chat_documents']} 个
- 有效相邻问答轮次：{scope['usable_adjacent_qa_turns']} 轮
- 有效首轮回复：{scope['first_reply_turns']} 轮
- 用于日常语言统计的卖家回复：{scope['conversational_style_answers']} 条
- 排除的长商品清单或超长回复：{scope['excluded_catalog_or_long_answer_count']} 条

## 语言特点

- 去空白字符长度：P25={metrics['compact_character_p25']}，中位数={metrics['compact_character_median']}，P75={metrics['compact_character_p75']}，P90={metrics['compact_character_p90']}。
- 单行回复：{metrics['single_line_count']} 条，占 {metrics['single_line_ratio']:.1%}。
- 使用句末标点：{metrics['terminal_punctuation_count']} 条，占 {metrics['terminal_punctuation_ratio']:.1%}。
- 追问型回复：{metrics['clarification_question_count']} 条，占 {metrics['clarification_question_ratio']:.1%}。
- “您好/亲/感谢咨询”等客服腔：{metrics['formal_service_phrase_count']} 条。
- 表情或感叹号：{metrics['emoji_or_exclamation_count']} 条。

## 常见短语

{phrase_lines}

## 对话逻辑

{logic_lines}

## 写入程序后的约束

{policy_lines}

## 数据充足度

现有记录足以约束日常售前问答的句长、称呼、标点、追问和分行方式。全量问答库中交易流程与退换售后样本明显少于规格、充电、续航和物流；若要进一步提高这些场景的本人相似度，建议后续补充近期真实的议价成交、拍下改价、付款催促、异常物流和售后协商记录。新增记录仍只学习口吻，商品事实以当前商品表为准。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir or args.source.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis = analyze(args.source)
    json_path = output_dir / "卖家聊天风格_分析报告.json"
    markdown_path = output_dir / "卖家聊天风格_分析报告.md"
    json_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    markdown_path.write_text(markdown_report(analysis), encoding="utf-8", newline="\n")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_seller_chat_style import analyze


def test_analyzer_uses_only_reliable_conversational_seller_turns(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.json"
    source.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "document_type": "product_chat",
                        "quality_flags": [],
                        "messages": [
                            {"direction": "incoming", "message_type": "text", "text": "这个能用吗"},
                            {"direction": "outgoing", "message_type": "text", "text": "可以"},
                        ],
                    },
                    {
                        "document_type": "product_chat",
                        "quality_flags": ["speaker_direction_may_be_unreliable"],
                        "messages": [
                            {"direction": "incoming", "message_type": "text", "text": "多少钱"},
                            {"direction": "outgoing", "message_type": "text", "text": "您好亲"},
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = analyze(source)

    assert result["scope"]["product_chat_documents"] == 2
    assert result["scope"]["reliable_product_chat_documents"] == 1
    assert result["scope"]["conversational_style_answers"] == 1
    assert result["voice_metrics"]["compact_character_median"] == 2
    assert result["voice_metrics"]["formal_service_phrase_count"] == 0

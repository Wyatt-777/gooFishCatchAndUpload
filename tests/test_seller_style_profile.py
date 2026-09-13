from __future__ import annotations

from xianyu_assistant.customer_service.seller_style_profile import (
    SELLER_DIALOGUE_PLAYBOOK,
    SELLER_STYLE_CONSTRAINTS,
    SELLER_STYLE_METRICS,
    SELLER_STYLE_REFERENCE_PHRASES,
)


def test_style_profile_matches_reviewed_corpus_aggregates() -> None:
    assert SELLER_STYLE_METRICS["reviewed_conversational_answers"] == 229
    assert SELLER_STYLE_METRICS["compact_character_median"] == 9
    assert SELLER_STYLE_METRICS["compact_character_p90"] == 31
    assert SELLER_STYLE_METRICS["formal_greeting_count"] == 0
    assert SELLER_STYLE_METRICS["emoji_or_exclamation_count"] == 0


def test_style_profile_separates_tone_from_current_product_facts() -> None:
    combined = "\n".join(
        (*SELLER_STYLE_CONSTRAINTS, *SELLER_DIALOGUE_PLAYBOOK, *SELLER_STYLE_REFERENCE_PHRASES)
    )
    assert "历史问答只学习说话顺序和口吻" in combined
    assert "当前商品资料" in combined
    assert "678" not in combined
    assert "398" not in combined
    assert "518" not in combined

"""Local policy checks for model proposals.

The model is only a wording assistant.  This module is the final local gate
before a proposal can enter the human-review queue.
"""

from __future__ import annotations

from dataclasses import dataclass

from xianyu_assistant.customer_service.models import (
    MediaAsset,
    ProductKnowledge,
    ReplyProposal,
)
from xianyu_assistant.customer_service.price_change import PricePolicyError, parse_money
from xianyu_assistant.customer_service.privacy import PrivacyRedactor


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The result of checking one structured model proposal."""

    allowed: bool
    proposal: ReplyProposal
    reason: str | None = None
    handoff: bool = False


class ReplyPolicy:
    """Apply deterministic rules that the model cannot override."""

    _HANDOFF_INTENTS = frozenset(
        {"handoff", "human", "人工", "投诉", "纠纷", "法律", "unsafe"}
    )

    def __init__(self, *, max_reply_text_length: int = 2_000) -> None:
        if max_reply_text_length <= 0:
            raise ValueError("max_reply_text_length 必须大于 0。")
        self._max_reply_text_length = max_reply_text_length

    def check(
        self,
        proposal: ReplyProposal,
        *,
        knowledge: ProductKnowledge | None,
        media_asset: MediaAsset | None,
    ) -> PolicyDecision:
        """Return a safe local decision without calling the browser or model."""
        text = proposal.reply_text.strip()
        if not text:
            return PolicyDecision(False, proposal, "模型返回了空回复。")
        if len(text) > self._max_reply_text_length:
            return PolicyDecision(False, proposal, "回复超过本地长度限制。")
        if proposal.requires_handoff or proposal.intent.strip().casefold() in self._HANDOFF_INTENTS:
            return PolicyDecision(
                False,
                proposal,
                proposal.handoff_reason or "模型要求转人工。",
                handoff=True,
            )

        # A model must never introduce a fresh personal identifier into an
        # outgoing message.  The redactor is used as a detector here; the
        # actual text is not silently rewritten.
        redactor = PrivacyRedactor()
        if redactor.redact(text) != text:
            return PolicyDecision(False, proposal, "回复包含未获准的个人敏感信息。")

        if proposal.media_asset_id is not None:
            if media_asset is None or not media_asset.enabled:
                return PolicyDecision(False, proposal, "模型选择了不存在或已停用的图片资源。")
            if knowledge is not None and media_asset.product_key not in {
                None,
                knowledge.product_key,
            }:
                return PolicyDecision(False, proposal, "图片资源与当前商品不匹配。")

        if proposal.offered_price is not None and knowledge is not None:
            try:
                offered = parse_money(proposal.offered_price, field_name="模型报价")
                minimum = (
                    parse_money(knowledge.minimum_price, field_name="商品最低价")
                    if knowledge.minimum_price
                    else None
                )
            except PricePolicyError as error:
                return PolicyDecision(False, proposal, str(error))
            if minimum is not None and offered < minimum:
                return PolicyDecision(False, proposal, "报价低于商品最低价。")

        return PolicyDecision(True, ReplyProposal(
            reply_text=text,
            media_asset_id=proposal.media_asset_id,
            intent=proposal.intent.strip() or "unknown",
            requires_handoff=False,
            handoff_reason=None,
            needs_clarification=proposal.needs_clarification,
            offered_price=proposal.offered_price,
            facts_used=proposal.facts_used,
        ))

"""Bounded, evidence-linked continuation of unanswered attribute questions.

This module never decides prices/orders or changes message identity. Persisted
questions are ordinary turn decisions linked to a verified outgoing message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from xianyu_assistant.customer_service.battery_models import model_matches, model_selections
from xianyu_assistant.customer_service.contact_policy import is_order_card
from xianyu_assistant.customer_service.fact_guard import (
    FactGuardError,
    ProductMatch,
    critical_answer,
    critical_fields,
)
from xianyu_assistant.customer_service.fingerprints import (
    content_fingerprint,
    normalize_for_matching,
)
from xianyu_assistant.customer_service.message_roles import role_is_trusted
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.semantic_analysis import analyze_customer_turn

QUESTION_VERSION = "reply-question-v1"
_MODEL_QUESTION = re.compile(
    r"(?:哪个|什么|哪种|哪一?个|哪款)\s*(?:电池)?(?:型号|规格)"
    r"|(?:型号|规格)\s*(?:是)?\s*(?:哪个|什么)"
)
_DETAIL_QUESTION = re.compile(r"版本|接口|插头|车型|车速|功率|尺寸|照片|图片|几款|几种")


def asks_battery_model(text: str) -> bool:
    """A request for already supplied V/Ah, not another genuine missing detail."""
    folded = normalize_for_matching(text)
    return bool(_MODEL_QUESTION.search(folded)) and not _DETAIL_QUESTION.search(folded)


def is_model_answer(query: str) -> bool:
    """Accept only an isolated selection, never a new question/price/order."""
    semantics = analyze_customer_turn(query)
    if (critical_fields(query) or semantics.is_negotiation or semantics.is_order_request
            or semantics.is_correction or semantics.questions or is_order_card(query)):
        return False
    folded = normalize_for_matching(query)
    matches = model_matches(query)
    if not matches:
        return False
    for match in reversed(matches):
        folded = folded[:match.start()] + " " + folded[match.end():]
    # Deliberately small vocabulary; arbitrary text is a new turn, not an
    # invitation to inherit an old question. A bare voltage/capacity is handled
    # by the existing partial-selection resolver, not by guessing here.
    remainder = re.sub(r"我要|我想要|就是|这个|型号|电池|要|是|的|呢|啊|呀|和|或", "", folded)
    return not re.sub(r"[\s，,。.!！?？、/]+", "", remainder)


@dataclass(frozen=True, slots=True)
class ReplyQuestion:
    raw_query: str
    fact_query: str
    current_message_keys: tuple[str, ...]
    source_message_keys: tuple[str, ...] = ()
    clarification_message_key: str | None = None
    continuation_turn_id: str | None = None

    @property
    def inherited(self) -> bool:
        return bool(self.source_message_keys)

    def as_data(self, snapshot: ConversationSnapshot, *, missing_model: bool = False) -> dict:
        visible = {m.message_key: m for m in snapshot.messages}
        return {
            "version": QUESTION_VERSION,
            "raw_query": self.raw_query,
            "fact_query": self.fact_query,
            "requested_fields": list(critical_fields(self.fact_query)),
            "current_message_keys": list(self.current_message_keys),
            "current_content_fingerprints": [
                content_fingerprint(visible[key].kind.value, visible[key].text or "")
                if key in visible else None for key in self.current_message_keys
            ],
            "source_message_keys": list(self.source_message_keys),
            "clarification_message_key": self.clarification_message_key,
            "continuation_turn_id": self.continuation_turn_id,
            "missing_fields": ["battery_model"] if missing_model else [],
            "platform_product_id": snapshot.platform_product_id,
            "merchant_account_id": snapshot.merchant_account_id,
            "platform_customer_id": snapshot.platform_customer_id,
        }


def resolve_reply_question(
    snapshot: ConversationSnapshot,
    current: tuple[ChatMessage, ...],
    query: str,
    pending: dict | None = None,
) -> ReplyQuestion:
    """Continue only the nearest, still unanswered merchant clarification."""
    result = ReplyQuestion(query, query, tuple(m.message_key for m in current))
    if not current or not is_model_answer(query):
        return result
    current_keys = set(result.current_message_keys)
    positions = [i for i, m in enumerate(snapshot.messages) if m.message_key in current_keys]
    if len(positions) != len(current_keys):
        return result
    prior = snapshot.messages[:min(positions)]
    # The first new message must follow the actual clarification. No unknown
    # role or intervening merchant response can be silently skipped.
    if not prior:
        return result
    clarification = prior[-1]
    if (clarification.direction is not MessageDirection.OUTGOING
            or not role_is_trusted(clarification.direction, clarification.role_source)
            or not asks_battery_model(clarification.text or "")):
        return result
    original_keys: tuple[str, ...]
    source_turn = None
    if pending is not None:
        if (pending.get("version") != QUESTION_VERSION
                or pending.get("clarification_message_key") != clarification.message_key
                or pending.get("missing_fields") != ["battery_model"]):
            return result
        for field in ("platform_product_id", "merchant_account_id", "platform_customer_id"):
            before = pending.get(field)
            if before != getattr(snapshot, field):
                return result
        original = str(pending.get("raw_query") or "")
        original_keys = tuple(pending.get("current_message_keys") or ())
        source_turn = pending.get("turn_id")
        visible = {m.message_key: m for m in prior}
        if any(k in visible and (visible[k].direction is not MessageDirection.INCOMING
                                 or not role_is_trusted(visible[k].direction, visible[k].role_source))
               for k in original_keys):
            return result
        hashes = pending.get("current_content_fingerprints") or ()
        if hashes and len(hashes) != len(original_keys):
            return result
        for key, expected_hash in zip(original_keys, hashes, strict=False):
            if (key in visible and expected_hash
                    and expected_hash != content_fingerprint(visible[key].kind.value,
                                                             visible[key].text or "")):
                return result
        if any(m.direction is MessageDirection.UNKNOWN for m in prior):
            return result
    else:
        # Upgrade/manual clarification compatibility uses only adjacent visible
        # messages, never the worker's unbounded historical question search.
        sources = []
        for message in reversed(prior[:-1]):
            if message.direction is not MessageDirection.INCOMING:
                break
            if (message.kind is not MessageKind.TEXT
                    or not role_is_trusted(message.direction, message.role_source)):
                return result
            sources.append(message)
        sources.reverse()
        original_keys = tuple(m.message_key for m in sources)
        original = " ".join(m.text or "" for m in sources)
    semantics = analyze_customer_turn(original)
    if (not original_keys or not critical_fields(original) or semantics.is_negotiation
            or semantics.is_order_request or is_order_card(original)):
        return result
    old_models = {canonical for _, canonical in model_selections(original)}
    new_models = {canonical for _, canonical in model_selections(query)}
    if old_models and old_models != new_models:
        return result
    models = " ".join(canonical for _, canonical in model_selections(query))
    return ReplyQuestion(query, models + " " + original, result.current_message_keys,
                         original_keys, clarification.message_key, source_turn)


def validate_question_reply(
    text: str, question: ReplyQuestion, match: ProductMatch, fact_check: dict,
) -> None:
    """Reject repeated model requests and unanswered inherited attributes."""
    supplied_model = (match.source == "current_customer_model"
                      and len(model_selections(question.raw_query)) == 1)
    if (len(match.products) == 1 or supplied_model) and asks_battery_model(text):
        raise FactGuardError("顾客已提供并匹配型号，不能再次询问同一型号")
    if not question.inherited:
        return
    expected = critical_answer(question.fact_query, match)
    if expected is None or expected.handoff or expected.clarification:
        return  # Existing missing/conflicting-product rules still govern.
    fields = set(critical_fields(question.fact_query))
    answered = {item["field"] for item in fact_check.get("evidence", [])}
    # Literal deterministic answers also cover condition/source facts whose
    # evidence field is more specific than the question (condition_二手).
    if (fields - answered
            and normalize_for_matching(expected.text) not in normalize_for_matching(text)):
        raise FactGuardError("回复没有回答顾客补充型号前尚未解决的商品属性问题")

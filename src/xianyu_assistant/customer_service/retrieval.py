"""Small local n-gram retriever for historical wording examples."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching
from xianyu_assistant.customer_service.models import HistoricalExample

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]")
_TRUST_WEIGHTS = {
    "imported_history": 0.85,
    "cleaned_history": 1.0,
    "conversation_knowledge": 1.1,
    "human_confirmed": 1.2,
    "user_curated": 1.5,
    "auto_generated": 0.0,
}
_DEDUPLICATION_PRIORITY = {
    "user_curated": 4,
    "human_confirmed": 3,
    "conversation_knowledge": 2,
    "cleaned_history": 2,
    "imported_history": 1,
    "auto_generated": 0,
}


@dataclass(frozen=True, slots=True)
class ScoredHistoricalExample:
    """A history example with its local retrieval score."""

    example: HistoricalExample
    score: float


def tokenize(text: str) -> tuple[str, ...]:
    """Create Chinese 2/3-grams plus word/number tokens."""
    normalized = normalize_for_matching(text)
    units = _TOKEN_RE.findall(normalized)
    tokens: list[str] = [unit for unit in units if len(unit) > 1 or unit.isascii()]
    chinese = "".join(unit for unit in units if len(unit) == 1 and not unit.isascii())
    tokens.extend(chinese[index : index + size] for size in (2, 3) for index in range(len(chinese) - size + 1))
    return tuple(tokens)


class HistoricalExampleRetriever:
    """Rank local Q&A items; callers decide whether results are facts or style."""

    def __init__(self, examples: Iterable[HistoricalExample]) -> None:
        self._examples = tuple(self._deduplicate(examples))
        self._document_tokens = [Counter(tokenize(example.customer_text)) for example in self._examples]
        self._idf = self._build_idf(self._document_tokens)

    def search(self, query: str, *, limit: int = 5) -> list[ScoredHistoricalExample]:
        """Return up to ``limit`` relevant examples, excluding auto-generated data."""
        if limit <= 0:
            return []
        query_tokens = Counter(tokenize(query))
        if not query_tokens:
            return []
        scored = [
            ScoredHistoricalExample(example, self._score(query_tokens, document_tokens, example))
            for example, document_tokens in zip(self._examples, self._document_tokens)
            if _TRUST_WEIGHTS.get(example.trust_level, 0.0) > 0
        ]
        scored.sort(key=lambda item: (-item.score, item.example.example_id))
        return [item for item in scored[:limit] if item.score > 0]

    @staticmethod
    def _build_idf(documents: Sequence[Counter[str]]) -> dict[str, float]:
        document_count = len(documents)
        frequencies = Counter(token for document in documents for token in document)
        return {
            token: math.log((document_count + 1) / (frequency + 1)) + 1
            for token, frequency in frequencies.items()
        }

    def _score(
        self,
        query: Counter[str],
        document: Counter[str],
        example: HistoricalExample,
    ) -> float:
        overlap = set(query) & set(document)
        if not overlap:
            return 0.0
        query_vector = {token: count * self._idf.get(token, 1.0) for token, count in query.items()}
        document_vector = {
            token: count * self._idf.get(token, 1.0) for token, count in document.items()
        }
        dot = sum(query_vector[token] * document_vector[token] for token in overlap)
        query_norm = math.sqrt(sum(value * value for value in query_vector.values()))
        document_norm = math.sqrt(sum(value * value for value in document_vector.values()))
        cosine = dot / (query_norm * document_norm) if query_norm and document_norm else 0.0
        jaccard = len(overlap) / len(set(query) | set(document))
        return (0.75 * cosine + 0.25 * jaccard) * _TRUST_WEIGHTS.get(example.trust_level, 0.0)

    @staticmethod
    def _deduplicate(examples: Iterable[HistoricalExample]) -> list[HistoricalExample]:
        # Prefer reviewed/cleaned wording when the same merchant reply also
        # exists in a raw history import.  Otherwise an older raw row can hide
        # every cleaned replacement before scoring even begins.
        prioritized = sorted(
            enumerate(examples),
            key=lambda item: (
                -_DEDUPLICATION_PRIORITY.get(item[1].trust_level, 0),
                item[0],
            ),
        )
        selected: list[HistoricalExample] = []
        seen_replies: list[set[str]] = []
        for _, example in prioritized:
            if example.trust_level == "auto_generated":
                continue
            normalized_reply = normalize_for_matching(example.merchant_text)
            if not normalized_reply:
                continue
            reply_tokens = set(tokenize(normalized_reply))
            if any(
                reply_tokens == existing
                or (
                    reply_tokens
                    and existing
                    and len(reply_tokens & existing) / len(reply_tokens | existing) >= 0.95
                )
                for existing in seen_replies
            ):
                continue
            selected.append(example)
            seen_replies.append(reply_tokens)
        return selected

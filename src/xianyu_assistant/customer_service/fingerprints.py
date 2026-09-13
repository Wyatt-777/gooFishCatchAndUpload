"""Stable, one-way fingerprints for message batches and import deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_matching(text: str) -> str:
    """Normalize harmless presentation differences without changing meaning."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def content_fingerprint(*parts: str) -> str:
    """Return a SHA-256 fingerprint for ordered, length-delimited parts."""
    digest = hashlib.sha256()
    for part in parts:
        encoded = normalize_for_matching(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def batch_fingerprint(
    conversation_key: str,
    incoming_messages: Iterable[tuple[str, str]],
) -> str:
    """Fingerprint a conversation and its ordered incoming message keys/content."""
    parts = [conversation_key]
    for message_key, text in incoming_messages:
        parts.extend((message_key, text))
    return content_fingerprint(*parts)

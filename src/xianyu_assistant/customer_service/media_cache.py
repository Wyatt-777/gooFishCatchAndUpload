"""Bounded media resolution for the current customer turn.

Browser work stays on the adapter boundary. Historical media is read only from
an existing cache; it never opens a transcript menu or captures an old image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from xianyu_assistant.customer_service.clock import SystemClock
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ImagePayload,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.privacy import PrivacyRedactor

logger = logging.getLogger(__name__)
PARSER_VERSION = "media-v1"
_STABLE_KEY_SOURCES = frozenset({"platform_attribute", "react_message_id"})
_CacheKey = tuple[str, str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class MediaMessageState:
    message_key: str
    kind: str
    status: str
    current: bool
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class MediaResolution:
    texts: dict[str, str]
    image: ImagePayload | None
    current_query: str
    derived_digest: str
    states: tuple[MediaMessageState, ...]
    unresolved_current_keys: tuple[str, ...]
    cache_hits: int = 0


class MediaContextCache:
    """Retain successful transcripts and cap in-memory image bytes."""

    def __init__(self, repository: Any, *, clock: Any = None) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()
        self._redactor = PrivacyRedactor()
        self._run_identity = "run-" + uuid4().hex
        self._transcripts: OrderedDict[_CacheKey, tuple[str, datetime]] = OrderedDict()
        self._images: OrderedDict[_CacheKey, tuple[ImagePayload, float]] = OrderedDict()
        self._failures: OrderedDict[_CacheKey, tuple[int, float]] = OrderedDict()
        self._image_bytes = 0

    def clear(self) -> None:
        self._transcripts.clear()
        self._images.clear()
        self._failures.clear()
        self._image_bytes = 0

    def resolve(
        self,
        snapshot: ConversationSnapshot,
        query_messages: Sequence[ChatMessage],
        adapter: Any,
    ) -> MediaResolution:
        """Resolve new media and explicitly report any unanswered media inputs."""
        self._prune()
        current = tuple(query_messages)
        if not current:
            tail = []
            for message in reversed(snapshot.messages):
                if message.direction is not MessageDirection.INCOMING:
                    break
                tail.append(message)
            current = tuple(reversed(tail))
        current_keys = {message.message_key for message in current}
        images = [m for m in current if m.direction is MessageDirection.INCOMING
                  and m.kind is MessageKind.IMAGE]
        selected_image_key = images[-1].message_key if images else None
        texts = {m.message_key: m.text or f"[{m.kind.value}]" for m in snapshot.messages}
        states: list[MediaMessageState] = []
        unresolved: list[str] = []
        selected_image: ImagePayload | None = None
        hits = 0
        # Current inputs get browser time before any historical cache lookup.
        ordered = list(current) + [m for m in snapshot.messages if m.message_key not in current_keys]
        seen: set[str] = set()
        for message in ordered:
            if message.message_key in seen:
                continue
            seen.add(message.message_key)
            if message.direction is not MessageDirection.INCOMING or message.kind not in {
                MessageKind.VOICE, MessageKind.IMAGE,
            }:
                continue
            key, persistent, cacheable = self._key(snapshot, message)
            is_current = message.message_key in current_keys
            cached_transcript = self._get_transcript(key, persistent) if message.kind is MessageKind.VOICE else None
            if cached_transcript is not None:
                texts[message.message_key] = cached_transcript
                states.append(MediaMessageState(message.message_key, "voice", "cache_hit", is_current))
                hits += 1
                continue
            if not is_current:
                texts[message.message_key] = (
                    "[历史语音未转写]" if message.kind is MessageKind.VOICE else "[顾客历史图片]"
                )
                states.append(MediaMessageState(message.message_key, message.kind.value,
                                                "history_placeholder", False))
                continue
            if message.kind is MessageKind.IMAGE and message.message_key != selected_image_key:
                texts[message.message_key] = "[当前图片尚未解析，需要人工一并查看]"
                states.append(MediaMessageState(message.message_key, "image", "multiple_images", True))
                unresolved.append(message.message_key)
                continue
            cached_image = self._images.get(key) if cacheable else None
            if message.kind is MessageKind.IMAGE and cached_image is not None:
                self._images.move_to_end(key)
                selected_image = cached_image[0]
                texts[message.message_key] = "[顾客当前图片]"
                states.append(MediaMessageState(message.message_key, "image", "cache_hit", True))
                hits += 1
                continue
            attempts, retry_at = self._failures.get(key, (0, 0.0))
            if attempts >= 3 or self._clock.monotonic() < retry_at:
                texts[message.message_key] = "[当前媒体读取未完成，需要人工处理]"
                states.append(MediaMessageState(message.message_key, message.kind.value,
                                                "retry_exhausted" if attempts >= 3 else "retry_wait",
                                                True, attempts))
                unresolved.append(message.message_key)
                continue
            try:
                if message.kind is MessageKind.VOICE:
                    transcript = adapter.request_voice_transcript(message.message_key)
                    if not transcript or not transcript.strip():
                        raise ValueError("语音转写尚未显示")
                    text = self._redactor.redact(transcript.strip())
                    texts[message.message_key] = text
                    if cacheable:
                        self._save_transcript(key, text, persistent)
                else:
                    selected_image = adapter.capture_incoming_image(message.message_key)
                    texts[message.message_key] = "[顾客当前图片]"
                    if cacheable:
                        self._save_image(key, selected_image)
                self._failures.pop(key, None)
                states.append(MediaMessageState(message.message_key, message.kind.value,
                                                "resolved", True, attempts + 1))
            except Exception as error:
                # Losing the browser executor must trigger infrastructure recovery,
                # rather than being mistaken for an untranscribable customer voice.
                if (isinstance(error, sqlite3.Error) or getattr(error, "infrastructure", False)
                        or getattr(adapter, "infrastructure_failure", False)):
                    raise
                attempts += 1
                self._failures[key] = (attempts, self._clock.monotonic() + (2 if attempts == 1 else 5))
                self._failures.move_to_end(key)
                texts[message.message_key] = "[当前媒体读取失败，需要人工处理]"
                states.append(MediaMessageState(message.message_key, message.kind.value,
                                                "failed", True, attempts))
                unresolved.append(message.message_key)
                logger.warning("CS_MEDIA_RESOLVE_FAILURE kind=%s type=%s attempt=%s",
                               message.kind.value, type(error).__name__, attempts)
        self._prune()
        query = self._redactor.redact(" ".join(texts.get(m.message_key, m.text or f"[{m.kind.value}]")
                                               for m in current))
        # This proof covers the current customer input. History pagination does
        # not change it; the model task key separately hashes its full prompt.
        proof = {
            "account": snapshot.merchant_account_id or self._run_identity,
            "conversation": snapshot.conversation_key,
            "parser_version": PARSER_VERSION,
            "inputs": [
                {"key": message.message_key, "kind": message.kind.value,
                 "source": getattr(message, "media_source_signature", None),
                 "text": texts.get(message.message_key, message.text or "")}
                for message in current
            ],
        }
        digest = hashlib.sha256(json.dumps(proof, ensure_ascii=False, sort_keys=True).encode())
        if selected_image is not None:
            digest.update(selected_image.message_key.encode())
            digest.update(selected_image.mime_type.encode())
            digest.update(selected_image.content)
        return MediaResolution(texts, selected_image, query, digest.hexdigest(), tuple(states),
                               tuple(dict.fromkeys(unresolved)), hits)

    def _key(self, snapshot: ConversationSnapshot, message: ChatMessage) -> tuple[_CacheKey, bool, bool]:
        account = snapshot.merchant_account_id or self._run_identity
        source = getattr(message, "media_source_signature", None)
        stable = message.key_source in _STABLE_KEY_SOURCES and not snapshot.conversation_key.startswith("dom-")
        if not source:
            source = hashlib.sha256(
                f"{message.kind.value}:{message.text or ''}:{message.platform_time or ''}:"
                f"{message.content_fingerprint or ''}".encode()
            ).hexdigest()
        key = (account, snapshot.conversation_key, message.message_key, message.kind.value,
               str(source), PARSER_VERSION)
        persistent = bool(snapshot.merchant_account_id and stable
                          and getattr(message, "media_source_signature", None))
        return key, persistent, stable

    def _get_transcript(self, key: _CacheKey, persistent: bool) -> str | None:
        value = self._transcripts.get(key)
        if value is not None:
            self._transcripts.move_to_end(key)
            return value[0]
        getter = getattr(self._repository, "get_media_cache", None)
        if persistent and callable(getter):
            row = getter(account_key=key[0], conversation_key=key[1], message_key=key[2],
                         media_kind=key[3], content_fingerprint=key[4], parser_version=key[5],
                         now=self._clock.now())
            if row and row.get("status") == "success" and row.get("result_text"):
                text = self._redactor.redact(str(row["result_text"]))
                expires = datetime.fromisoformat(str(row["expires_at"]))
                self._transcripts[key] = (text, expires)
                return text
        return None

    def _save_transcript(self, key: _CacheKey, text: str, persistent: bool) -> None:
        now = self._clock.now()
        expires = now + timedelta(days=15)
        saver = getattr(self._repository, "save_media_cache", None)
        if persistent and callable(saver):
            saver(account_key=key[0], conversation_key=key[1], message_key=key[2],
                  media_kind=key[3], content_fingerprint=key[4], parser_version=key[5],
                  status="success", result_text=text, expires_at=expires, updated_at=now,
                  attempt_count=1)
        self._transcripts[key] = (text, expires)
        self._transcripts.move_to_end(key)

    def _save_image(self, key: _CacheKey, image: ImagePayload) -> None:
        existing = self._images.pop(key, None)
        if existing is not None:
            self._image_bytes -= len(existing[0].content)
        if len(image.content) > 32 * 1024 * 1024:
            return
        self._images[key] = (image, self._clock.monotonic() + 600)
        self._image_bytes += len(image.content)
        self._prune()

    def _prune(self) -> None:
        now = self._clock.now()
        monotonic_now = self._clock.monotonic()
        for key, (_, expires) in list(self._transcripts.items()):
            if expires <= now:
                self._transcripts.pop(key, None)
        while len(self._transcripts) > 5_000:
            self._transcripts.popitem(last=False)
        for key, (image, expires) in list(self._images.items()):
            if expires <= monotonic_now:
                self._images.pop(key)
                self._image_bytes -= len(image.content)
        while self._image_bytes > 32 * 1024 * 1024 or len(self._images) > 100:
            _, (image, _) = self._images.popitem(last=False)
            self._image_bytes -= len(image.content)
        while len(self._failures) > 5_000:
            self._failures.popitem(last=False)

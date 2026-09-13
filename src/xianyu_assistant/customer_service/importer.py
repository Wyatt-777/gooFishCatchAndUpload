"""Safe local import of historical Xianyu chat exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from xianyu_assistant.customer_service.models import (
    HistoricalExample,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.privacy import PrivacyRedactor


class ChatImportError(ValueError):
    """Raised when a history file is unsupported, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class ImportLimits:
    """Bounds applied before reading any archive member into memory."""

    max_archive_bytes: int = 100 * 1024 * 1024
    max_member_bytes: int = 50 * 1024 * 1024
    max_total_uncompressed_bytes: int = 150 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    message_key: str
    direction: MessageDirection
    kind: MessageKind
    text: str
    platform_message_id: str | None = None
    fingerprint: str | None = None
    platform_time: str | None = None
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ImportedConversation:
    conversation_key: str
    display_name: str | None
    platform_product_id: str | None
    messages: tuple[ImportedMessage, ...]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    source_name: str
    source_sha256: str
    conversation_count: int
    message_count: int
    customer_message_count: int
    merchant_message_count: int
    customer_turn_count: int
    answered_turn_count: int
    unique_customer_text_count: int
    unique_merchant_text_count: int
    example_count: int
    error_count: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportBundle:
    source_name: str
    source_sha256: str
    conversations: tuple[ImportedConversation, ...]
    examples: tuple[HistoricalExample, ...]
    errors: tuple[str, ...] = ()

    @property
    def preview(self) -> ImportPreview:
        messages = [message for conversation in self.conversations for message in conversation.messages]
        incoming = [message.text for message in messages if message.direction is MessageDirection.INCOMING]
        outgoing = [message.text for message in messages if message.direction is MessageDirection.OUTGOING]
        customer_turn_count = 0
        for conversation in self.conversations:
            previous_direction: MessageDirection | None = None
            for message in conversation.messages:
                if (
                    message.direction is MessageDirection.INCOMING
                    and previous_direction is not MessageDirection.INCOMING
                ):
                    customer_turn_count += 1
                previous_direction = message.direction
        return ImportPreview(
            source_name=self.source_name,
            source_sha256=self.source_sha256,
            conversation_count=len(self.conversations),
            message_count=len(messages),
            customer_message_count=len(incoming),
            merchant_message_count=len(outgoing),
            customer_turn_count=customer_turn_count,
            answered_turn_count=len(self.examples),
            unique_customer_text_count=len(set(incoming)),
            unique_merchant_text_count=len(set(outgoing)),
            example_count=len(self.examples),
            error_count=len(self.errors),
            errors=self.errors,
        )


class ChatHistoryImporter:
    """Parse JSON/CSV directly or a bounded ZIP without extracting it."""

    def __init__(self, *, limits: ImportLimits | None = None) -> None:
        self._limits = limits or ImportLimits()

    def preview(self, source_path: Path) -> ImportPreview:
        """Read and parse a source for UI preview without touching SQLite."""
        return self.parse(source_path).preview

    def parse(self, source_path: Path) -> ImportBundle:
        """Return redacted conversations and examples from a local source file."""
        if not source_path.is_file():
            raise ChatImportError(f"找不到聊天导入文件：{source_path}")
        payload, logical_name = self._read_source(source_path)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        redactor = PrivacyRedactor()
        errors: list[str] = []
        if logical_name.lower().endswith(".json"):
            conversations = self._parse_json(payload, redactor, errors)
        elif logical_name.lower().endswith(".csv"):
            conversations = self._parse_csv(payload, redactor, errors)
        else:
            raise ChatImportError("仅支持 ZIP、JSON 或 CSV 聊天记录。")
        examples = build_historical_examples(conversations)
        return ImportBundle(
            source_name=source_path.name,
            source_sha256=source_sha256,
            conversations=tuple(conversations),
            examples=examples,
            errors=tuple(errors),
        )

    def _read_source(self, source_path: Path) -> tuple[bytes, str]:
        suffix = source_path.suffix.casefold()
        if suffix != ".zip":
            size = source_path.stat().st_size
            if size > self._limits.max_member_bytes:
                raise ChatImportError("导入文件超过单文件大小限制。")
            return source_path.read_bytes(), source_path.name
        if source_path.stat().st_size > self._limits.max_archive_bytes:
            raise ChatImportError("ZIP 压缩包超过大小限制。")
        try:
            with zipfile.ZipFile(source_path) as archive:
                candidates: list[zipfile.ZipInfo] = []
                total_size = 0
                for info in archive.infolist():
                    self._validate_member(info)
                    if info.is_dir():
                        continue
                    total_size += info.file_size
                    if total_size > self._limits.max_total_uncompressed_bytes:
                        raise ChatImportError("ZIP 展开后的总大小超过安全限制。")
                    if Path(info.filename).suffix.casefold() in {".json", ".csv"}:
                        candidates.append(info)
                if not candidates:
                    raise ChatImportError("ZIP 中没有可导入的 JSON 或 CSV 文件。")
                candidates.sort(key=lambda info: (Path(info.filename).suffix.casefold() != ".json", info.filename))
                selected = candidates[0]
                if selected.file_size > self._limits.max_member_bytes:
                    raise ChatImportError("ZIP 成员文件超过单文件大小限制。")
                return archive.read(selected), selected.filename
        except zipfile.BadZipFile as error:
            raise ChatImportError("ZIP 文件损坏或格式无效。") from error

    def _validate_member(self, info: zipfile.ZipInfo) -> None:
        normalized_name = info.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if "\x00" in normalized_name or path.is_absolute() or ".." in path.parts:
            raise ChatImportError("ZIP 包含不安全的路径。")
        if info.file_size < 0 or info.compress_size < 0:
            raise ChatImportError("ZIP 包含无效的文件大小。")

    def _parse_json(
        self, payload: bytes, redactor: PrivacyRedactor, errors: list[str]
    ) -> list[ImportedConversation]:
        data = _decode_json(payload)
        if isinstance(data, Mapping) and isinstance(data.get("conversations"), list):
            return self._parse_conversation_mappings(data["conversations"], redactor, errors)
        if isinstance(data, list) and any(isinstance(item, Mapping) and "messages" in item for item in data):
            return self._parse_conversation_mappings(data, redactor, errors)
        if isinstance(data, list):
            return self._parse_flat_rows(data, redactor, errors)
        raise ChatImportError("JSON 必须是 conversations 层级结构或消息数组。")

    def _parse_csv(
        self, payload: bytes, redactor: PrivacyRedactor, errors: list[str]
    ) -> list[ImportedConversation]:
        text = _decode_text(payload)
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise ChatImportError("CSV 缺少表头。")
        return self._parse_flat_rows(reader, redactor, errors)

    def _parse_conversation_mappings(
        self, rows: Iterable[object], redactor: PrivacyRedactor, errors: list[str]
    ) -> list[ImportedConversation]:
        conversations: list[ImportedConversation] = []
        for index, raw_conversation in enumerate(rows):
            if not isinstance(raw_conversation, Mapping):
                errors.append(f"第 {index + 1} 个会话不是对象。")
                continue
            messages = raw_conversation.get("messages")
            if not isinstance(messages, list):
                errors.append(f"会话 {index + 1} 缺少 messages 数组。")
                continue
            conversation_key = _first_value(
                raw_conversation, "platform_conversation_id", "conversation_key", "conversation_id", "conversation_db_id"
            ) or f"conversation-{index + 1}"
            parsed_messages = self._parse_message_rows(
                conversation_key, messages, redactor, errors, source_label=f"会话 {index + 1}"
            )
            conversations.append(
                ImportedConversation(
                    conversation_key=conversation_key,
                    display_name=_first_value(raw_conversation, "customer_display_name", "display_name"),
                    platform_product_id=_first_value(
                        raw_conversation, "listing_id", "platform_product_id", "product_id"
                    ),
                    messages=tuple(parsed_messages),
                )
            )
        return conversations

    def _parse_flat_rows(
        self, rows: Iterable[object], redactor: PrivacyRedactor, errors: list[str]
    ) -> list[ImportedConversation]:
        grouped: OrderedDict[str, list[object]] = OrderedDict()
        metadata: dict[str, Mapping[str, object]] = {}
        for index, raw_row in enumerate(rows):
            if not isinstance(raw_row, Mapping):
                errors.append(f"第 {index + 1} 条消息不是对象。")
                continue
            conversation_key = _first_value(
                raw_row, "platform_conversation_id", "conversation_key", "conversation_id", "conversation_db_id"
            )
            if not conversation_key:
                errors.append(f"第 {index + 1} 条消息缺少会话标识。")
                continue
            grouped.setdefault(conversation_key, []).append(raw_row)
            metadata.setdefault(conversation_key, raw_row)
        conversations: list[ImportedConversation] = []
        for conversation_key, message_rows in grouped.items():
            messages = self._parse_message_rows(
                conversation_key, message_rows, redactor, errors, source_label=conversation_key
            )
            source = metadata[conversation_key]
            conversations.append(
                ImportedConversation(
                    conversation_key=conversation_key,
                    display_name=_first_value(source, "customer_display_name", "display_name"),
                    platform_product_id=_first_value(source, "listing_id", "platform_product_id", "product_id"),
                    messages=tuple(messages),
                )
            )
        return conversations

    def _parse_message_rows(
        self,
        conversation_key: str,
        rows: Iterable[object],
        redactor: PrivacyRedactor,
        errors: list[str],
        *,
        source_label: str,
    ) -> list[ImportedMessage]:
        messages: list[ImportedMessage] = []
        for index, raw_message in enumerate(rows):
            if not isinstance(raw_message, Mapping):
                errors.append(f"{source_label}第 {index + 1} 条消息不是对象。")
                continue
            text = _first_value(
                raw_message, "normalized_content", "raw_content", "text", "content", "message"
            )
            direction = _parse_direction(_first_value(raw_message, "direction", "sender_type"))
            if not text or direction is None:
                errors.append(f"{source_label}第 {index + 1} 条消息缺少文本或方向。")
                continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            message_key = _first_value(
                raw_message, "platform_message_id", "message_key", "fingerprint", "message_db_id"
            ) or hashlib.sha256(f"{conversation_key}:{index}:{text}".encode()).hexdigest()
            messages.append(
                ImportedMessage(
                    message_key=message_key,
                    direction=direction,
                    kind=_parse_kind(_first_value(raw_message, "message_type", "kind")),
                    text=redactor.redact(text),
                    platform_message_id=_first_value(raw_message, "platform_message_id"),
                    fingerprint=_first_value(raw_message, "fingerprint"),
                    platform_time=_first_value(raw_message, "platform_time"),
                    observed_at=_first_value(raw_message, "observed_at"),
                )
            )
        return messages


def build_historical_examples(
    conversations: Iterable[ImportedConversation],
) -> tuple[HistoricalExample, ...]:
    """Build redacted Q&A style samples from normalized conversations."""
    return tuple(_build_examples(conversations))


def _build_examples(conversations: Iterable[ImportedConversation]) -> list[HistoricalExample]:
    examples: list[HistoricalExample] = []
    for conversation in conversations:
        messages = conversation.messages
        index = 0
        while index < len(messages):
            if messages[index].direction is not MessageDirection.INCOMING:
                index += 1
                continue
            question_start = index
            while index < len(messages) and messages[index].direction is MessageDirection.INCOMING:
                index += 1
            if index >= len(messages) or messages[index].direction is not MessageDirection.OUTGOING:
                continue
            answer_start = index
            while index < len(messages) and messages[index].direction is MessageDirection.OUTGOING:
                index += 1
            question = "\n".join(message.text for message in messages[question_start:answer_start])
            answer = "\n".join(message.text for message in messages[answer_start:index])
            example_id = hashlib.sha256(
                f"{conversation.conversation_key}\x00"
                f"{messages[question_start].message_key}\x00"
                f"{messages[answer_start].message_key}".encode()
            ).hexdigest()
            examples.append(
                HistoricalExample(
                    example_id=example_id,
                    customer_text=question,
                    merchant_text=answer,
                    trust_level="imported_history",
                )
            )
    return examples


def _decode_json(payload: bytes) -> object:
    try:
        return json.loads(_decode_text(payload))
    except json.JSONDecodeError as error:
        raise ChatImportError(f"JSON 格式无效：{error.msg}") from error


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ChatImportError("文件编码不受支持。")


def _first_value(row: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _parse_direction(value: str | None) -> MessageDirection | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"incoming", "in", "received", "customer"}:
        return MessageDirection.INCOMING
    if normalized in {"outgoing", "out", "sent", "merchant", "seller"}:
        return MessageDirection.OUTGOING
    return None


def _parse_kind(value: str | None) -> MessageKind:
    if value is None:
        return MessageKind.TEXT
    normalized = value.casefold()
    return {
        "text": MessageKind.TEXT,
        "image": MessageKind.IMAGE,
        "voice": MessageKind.VOICE,
    }.get(normalized, MessageKind.UNKNOWN)

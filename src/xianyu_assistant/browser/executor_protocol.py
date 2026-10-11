"""Bounded JSON messages for the isolated, local browser executor."""

from __future__ import annotations

import base64
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from xianyu_assistant.browser.manager import BrowserConnectionConfig, BrowserConnectionError
from xianyu_assistant.customer_service import models
from xianyu_assistant.customer_service.browser_adapter import (
    ChatAdapterError,
    ChatPageContract,
    ChatPageSelectors,
    ConversationIdentityError,
    ConversationLookupError,
    ReadOnlyAdapterError,
)
from xianyu_assistant.customer_service.protocols import (
    PriceChangeNotPerformedError,
    TextSendNotPerformedError,
)
from xianyu_assistant.customer_service.send_reliability import safe_error

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
PROTOCOL_VERSION = 1
_DATA_TYPES = {
    item.__name__: item for item in (
        BrowserConnectionConfig, ChatPageContract, ChatPageSelectors,
        models.ConversationSummary, models.ChatMessage, models.PlatformSystemEvent,
        models.ConversationSnapshot, models.ImagePayload, models.PageHealth,
        models.SendReceipt, models.PriceChangeReceipt,
    )
}
_ENUM_TYPES = {
    item.__name__: item for item in (
        models.MessageDirection, models.MessageKind, models.PageHealthStatus,
        models.PlatformSystemEventKind,
    )
}


class BrowserExecutorError(BrowserConnectionError):
    """An executor/transport failure requiring recovery before more DOM actions."""

    infrastructure_error = True
    infrastructure = True

    def __init__(self, message: str, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic or {}


class BrowserOperationTimeout(BrowserExecutorError):
    """The parent deadline expired; the actor has been isolated."""


class BrowserOperationCancelled(BrowserExecutorError):
    """The operator stopped the executor; submissions retain their evidence."""


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        if type(value).__name__ not in _ENUM_TYPES:
            raise ValueError("Unsupported browser IPC enum")
        return {"$enum": type(value).__name__, "value": value.value}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if is_dataclass(value) and not isinstance(value, type):
        if type(value).__name__ not in _DATA_TYPES:
            raise ValueError("Unsupported browser IPC data type")
        return {"$data": type(value).__name__, "fields": {
            field.name: _encode(getattr(value, field.name)) for field in fields(value)
        }}
    if isinstance(value, tuple):
        return {"$tuple": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _encode(item) for key, item in value.items()}
    raise ValueError("Unsupported browser IPC value")


def _decode(value: Any, depth: int = 0) -> Any:
    if depth > 40:
        raise ValueError("Browser IPC nesting limit exceeded")
    if isinstance(value, list):
        return [_decode(item, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    if "$data" in value:
        data_type = _DATA_TYPES.get(value["$data"])
        raw_fields = value.get("fields")
        if data_type is None or not isinstance(raw_fields, dict):
            raise ValueError("Invalid browser IPC data type")
        if set(raw_fields) != {field.name for field in fields(data_type)}:
            raise ValueError("Invalid browser IPC data fields")
        return data_type(**{key: _decode(item, depth + 1) for key, item in raw_fields.items()})
    if "$enum" in value:
        enum_type = _ENUM_TYPES.get(value["$enum"])
        if enum_type is None:
            raise ValueError("Invalid browser IPC enum")
        return enum_type(value["value"])
    if "$bytes" in value:
        return base64.b64decode(value["$bytes"], validate=True)
    if "$datetime" in value:
        return datetime.fromisoformat(value["$datetime"])
    if "$path" in value:
        return Path(value["$path"])
    if "$tuple" in value:
        return tuple(_decode(item, depth + 1) for item in value["$tuple"])
    return {key: _decode(item, depth + 1) for key, item in value.items()}


def pack_message(message: dict[str, Any]) -> bytes:
    raw = json.dumps(_encode(message), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Browser IPC payload is too large")
    return raw


def unpack_message(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("Browser IPC payload is too large")
    result = _decode(json.loads(raw.decode("utf-8")))
    if not isinstance(result, dict):
        raise TypeError("Browser IPC message must be an object")
    return result


def encode_error(error: Exception) -> dict[str, Any]:
    return {
        "type": type(error).__name__, "message": safe_error(str(error))[:900],
        "diagnostic": getattr(error, "diagnostic", {}),
        "retryable": bool(getattr(error, "retryable", False)),
    }


def decode_error(payload: dict[str, Any]) -> Exception:
    name = payload.get("type")
    detail = str(payload.get("message") or "浏览器操作失败。")
    diagnostic = dict(payload.get("diagnostic") or {})
    if name == "TextSendNotPerformedError":
        return TextSendNotPerformedError(detail, retryable=bool(payload.get("retryable")))
    if name == "PriceChangeNotPerformedError":
        return PriceChangeNotPerformedError(detail)
    if name in {"ConversationIdentityError", "ConversationLookupError"}:
        cls = ConversationIdentityError if name == "ConversationIdentityError" else ConversationLookupError
        return cls(detail, diagnostic)
    if name == "ReadOnlyAdapterError":
        return ReadOnlyAdapterError(detail)
    if name == "BrowserConnectionError":
        return BrowserExecutorError(detail, diagnostic)
    if name in {"BrowserExecutorError", "BrowserOperationTimeout", "BrowserOperationCancelled"}:
        cls = {"BrowserExecutorError": BrowserExecutorError,
               "BrowserOperationTimeout": BrowserOperationTimeout,
               "BrowserOperationCancelled": BrowserOperationCancelled}[name]
        return cls(detail, diagnostic)
    return ChatAdapterError(detail)

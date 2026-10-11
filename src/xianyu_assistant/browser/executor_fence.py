"""Persistent ownership proof prevents a new sender while an old actor may live."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from xianyu_assistant.browser.executor_protocol import BrowserExecutorError
from xianyu_assistant.browser.windows_job import named_job_is_empty, process_identity


def load_retired_fence(path: Path) -> dict[str, Any] | None:
    """Return only a provably exited owner; never kill a PID found in a file."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (payload.get("version") != 1 or not isinstance(payload.get("job_name"), str)
                or not payload["job_name"].startswith("Local\\XianyuAssistantBrowser-")
                or not isinstance(payload.get("processes"), list)):
            raise BrowserExecutorError("旧浏览器执行者身份记录不完整，禁止新增发送。")
        if not named_job_is_empty(payload["job_name"]):
            raise BrowserExecutorError("旧浏览器执行者仍在运行，禁止启动第二个发送通道。")
        for identity in payload["processes"]:
            if (not isinstance(identity, dict) or not isinstance(identity.get("pid"), int)
                    or not isinstance(identity.get("created_filetime"), int)):
                raise BrowserExecutorError("旧浏览器执行者身份无法核对，禁止新增发送。")
            current = process_identity(identity["pid"])
            if current == identity:
                raise BrowserExecutorError("旧浏览器执行者退出尚未确认，禁止新增发送。")
        return payload
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise BrowserExecutorError("无法确认旧浏览器执行者已退出，发送通道保持暂停。") from error


def write_fence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

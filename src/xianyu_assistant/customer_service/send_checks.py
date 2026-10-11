"""Eligibility rules for read-only checks; never authorize a new submission."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

SEND_CHECK_WINDOW_SECONDS = 90
MAX_SEND_CHECKS = 2


def send_check_block_reason(row: dict[str, Any], now: datetime) -> str | None:
    """Reject expired/inconsistent evidence before opening a historic chat."""
    try:
        evidence = json.loads(row.get("evidence_json") or "{}")
        if not isinstance(evidence, dict):
            raise TypeError("evidence is not an object")
        baseline = evidence.get("baseline_keys")
        if not isinstance(baseline, list) or not baseline or not all(
            isinstance(key, str) and key for key in baseline
        ):
            return "提交基线缺失或无效，保留人工核对，禁止重发。"
        if evidence.get("conversation_key") != row.get("conversation_key"):
            return "提交证据与核对会话不一致，保留人工核对，禁止重发。"
        submitted = datetime.fromisoformat(str(evidence.get("submitted_at", "")))
        if submitted.tzinfo is None or now.tzinfo is None:
            return "提交时间缺少时区，保留人工核对，禁止重发。"
        age = now - submitted
        if age < timedelta(seconds=-5):
            return "提交时间超前，保留人工核对，禁止重发。"
        if age > timedelta(seconds=SEND_CHECK_WINDOW_SECONDS):
            return "提交证据已超过 90 秒核对窗口，保留人工核对，禁止重发。"
        if int(row.get("recheck_count", 0)) >= MAX_SEND_CHECKS:
            return "自动核对次数已用完，保留人工核对，禁止重发。"
    except (ValueError, TypeError, OverflowError):
        return "提交证据无法解析，保留人工核对，禁止重发。"
    return None

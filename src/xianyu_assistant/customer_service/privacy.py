"""Local redaction for customer-service prompts, audit, and imported history."""

from __future__ import annotations

import re
from collections import defaultdict


class PrivacyRedactor:
    """Replace common personal data with stable request-local placeholders."""

    _FIELD_PATTERNS = (
        ("地址", re.compile(r"(?P<label>收货地址|收件地址|地址)\s*[:：]?\s*(?P<value>[^\n，,。；;]+)")),
        (
            "收件人",
            re.compile(
                r"(?P<label>收件人|联系人|姓名)\s*[:：]\s*"
                r"(?P<value>[^\n，,。；;]+?(?=\s+(?:收货地址|收件地址|地址)\s*[:：]|$))"
            ),
        ),
    )
    _EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)(?![\w.-])")
    _ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
    _BANK_CARD_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
    _MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
    _LANDLINE_RE = re.compile(r"(?<!\d)(0\d{2,3}[- ]?\d{7,8})(?!\d)")
    _LONG_NUMBER_RE = re.compile(r"(?<!\d)(\d{12,30})(?!\d)")

    def __init__(self) -> None:
        self._values: dict[str, dict[str, str]] = defaultdict(dict)
        self._counters: defaultdict[str, int] = defaultdict(int)

    def redact(self, text: str) -> str:
        """Redact sensitive fields and identifiers while keeping repeated values referable."""
        redacted = text
        for category, pattern in self._FIELD_PATTERNS:
            redacted = pattern.sub(
                lambda match, category=category: (
                    f"{match.group('label')}：{self._placeholder(category, match.group('value'))}"
                ),
                redacted,
            )
        redacted = self._replace(category="邮箱", pattern=self._EMAIL_RE, text=redacted)
        redacted = self._replace(category="身份证", pattern=self._ID_CARD_RE, text=redacted)
        redacted = self._replace(category="银行卡", pattern=self._BANK_CARD_RE, text=redacted)
        redacted = self._replace(category="手机号", pattern=self._MOBILE_RE, text=redacted)
        redacted = self._replace(category="座机", pattern=self._LANDLINE_RE, text=redacted)
        return self._replace(category="长数字", pattern=self._LONG_NUMBER_RE, text=redacted)

    def redact_many(self, texts: list[str]) -> list[str]:
        """Redact a list in one request scope so repeated values share placeholders."""
        return [self.redact(text) for text in texts]

    def _replace(self, *, category: str, pattern: re.Pattern[str], text: str) -> str:
        return pattern.sub(lambda match: self._placeholder(category, match.group(1)), text)

    def _placeholder(self, category: str, value: str) -> str:
        normalized = value.strip()
        if normalized not in self._values[category]:
            self._counters[category] += 1
            self._values[category][normalized] = f"[{category}_{self._counters[category]}]"
        return self._values[category][normalized]

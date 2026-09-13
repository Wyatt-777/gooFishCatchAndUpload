"""Privacy-aware local diagnostics for read-only chat-page adapter failures."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from xianyu_assistant.customer_service.privacy import PrivacyRedactor


class DiagnosticPage(Protocol):
    def content(self) -> str:
        """Return the current DOM HTML."""

    def screenshot(self, **kwargs: object) -> bytes:
        """Return a local screenshot without uploading it."""


class ChatAdapterDiagnostics:
    """Write bounded, local diagnostics without logging chat content."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path.cwd() / "debug" / "customer_service"

    def record_failure(self, page: DiagnosticPage | None, reason: str) -> None:
        """Save redacted DOM text and a screenshot when the page is available."""
        if page is None:
            return
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        safe_reason = PrivacyRedactor().redact(reason)[:500]
        try:
            dom = PrivacyRedactor().redact(page.content())[:20_000]
            digest = hashlib.sha256(dom.encode("utf-8")).hexdigest()[:16]
            directory = self._root / f"{stamp}-{digest}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "dom_summary.txt").write_text(dom, encoding="utf-8")
            (directory / "metadata.json").write_text(
                json.dumps({"reason": safe_reason, "dom_sha256_prefix": digest}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (directory / "page.png").write_bytes(page.screenshot(type="png"))
        except (AttributeError, OSError, TypeError, ValueError):
            # Diagnostics must never interrupt the read-only worker.
            return

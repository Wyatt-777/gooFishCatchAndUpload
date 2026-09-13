"""CS-3 tests for bounded local adapter diagnostics."""

import json
from pathlib import Path

from xianyu_assistant.customer_service.diagnostics import ChatAdapterDiagnostics


class FakeDiagnosticPage:
    def content(self) -> str:
        return "<body>手机号 13800138000 地址：上海市浦东新区</body>"

    def screenshot(self, **_kwargs: object) -> bytes:
        return b"fixture-png"


def test_diagnostics_redact_dom_summary_and_keep_screenshot_local(tmp_path: Path) -> None:
    ChatAdapterDiagnostics(tmp_path).record_failure(FakeDiagnosticPage(), "读取手机号 13800138000 失败")

    metadata_path = next(tmp_path.glob("*/metadata.json"))
    directory = metadata_path.parent
    summary = (directory / "dom_summary.txt").read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "13800138000" not in summary
    assert "13800138000" not in json.dumps(metadata, ensure_ascii=False)
    assert (directory / "page.png").read_bytes() == b"fixture-png"

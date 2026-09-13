"""Checks for local CDP connection settings."""

from pathlib import Path

from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.ui.browser_connection_panel import (
    BrowserConnectionPanel,
    _default_cdp_port,
)


def test_default_cdp_port_uses_valid_local_launcher_override(monkeypatch: object) -> None:
    """A launcher can use a non-default port without changing source defaults."""
    monkeypatch.setenv("XIANYU_CDP_PORT", "9223")  # type: ignore[union-attr]

    assert _default_cdp_port() == 9223


def test_default_cdp_port_rejects_invalid_launcher_override(monkeypatch: object) -> None:
    """Malformed environment input always falls back to the documented default."""
    monkeypatch.setenv("XIANYU_CDP_PORT", "not-a-port")  # type: ignore[union-attr]

    assert _default_cdp_port() == 9333


def test_browser_cdp_port_is_persisted_and_reused(
    qapp: object,
    tmp_path: Path,
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()

    first = BrowserConnectionPanel(repository)
    assert first.port_input.value() == 9333
    first.port_input.setValue(9333)
    first.save_connection_settings()

    second = BrowserConnectionPanel(repository)
    assert second.port_input.value() == 9333
    assert repository.get_setting("browser_cdp_port") == "9333"

"""Checks for local CDP connection settings."""

from xianyu_assistant.ui.browser_connection_panel import _default_cdp_port


def test_default_cdp_port_uses_valid_local_launcher_override(monkeypatch: object) -> None:
    """A launcher can use a non-default port without changing source defaults."""
    monkeypatch.setenv("XIANYU_CDP_PORT", "9223")  # type: ignore[union-attr]

    assert _default_cdp_port() == 9223


def test_default_cdp_port_rejects_invalid_launcher_override(monkeypatch: object) -> None:
    """Malformed environment input always falls back to the documented default."""
    monkeypatch.setenv("XIANYU_CDP_PORT", "not-a-port")  # type: ignore[union-attr]

    assert _default_cdp_port() == 9222

"""Unit tests for CDP configuration and the browser resource boundary."""

from __future__ import annotations

import asyncio

import pytest

from xianyu_assistant.browser import manager as manager_module
from xianyu_assistant.browser.manager import (
    DEFAULT_CDP_PORT,
    XIANYU_HOME_URL,
    BrowserConnectionConfig,
    BrowserConnectionError,
    BrowserManager,
)


def test_default_cdp_port_matches_the_persisted_browser_setup() -> None:
    assert BrowserConnectionConfig().port == DEFAULT_CDP_PORT == 9333


class FakePage:
    """Small Page double that records navigation requests."""

    url = "about:blank"

    def __init__(self) -> None:
        self.brought_to_front = False

    def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    def bring_to_front(self) -> None:
        self.brought_to_front = True


class FakeContext:
    """Default browser context supplied by the fake CDP browser."""

    def __init__(self) -> None:
        self.pages = [FakePage()]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class FakeBrowser:
    """CDP Browser double with exactly one default context."""

    def __init__(self) -> None:
        self.contexts = [FakeContext()]


class FakeChromium:
    """Capture the arguments used to attach over CDP."""

    def __init__(self) -> None:
        self.last_endpoint = ""
        self.last_options: dict[str, object] = {}
        self.browser = FakeBrowser()

    def connect_over_cdp(self, endpoint: str, **options: object) -> FakeBrowser:
        self.last_endpoint = endpoint
        self.last_options = options
        return self.browser


class FakePlaywright:
    """Playwright double that confirms shutdown does not close the browser."""

    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    """Match the object returned by ``sync_playwright`` before ``start``."""

    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright

    def start(self) -> FakePlaywright:
        return self._playwright


def test_cdp_connection_opens_xianyu_without_closing_user_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection must be local, preserve defaults, and only stop Playwright."""
    fake_playwright = FakePlaywright()
    monkeypatch.setattr(
        manager_module,
        "sync_playwright",
        lambda: FakePlaywrightStarter(fake_playwright),
    )
    manager = BrowserManager(BrowserConnectionConfig(port=9333))

    manager.connect()
    page_url = manager.open_xianyu()
    manager.disconnect()

    assert fake_playwright.chromium.last_endpoint == "http://localhost:9333"
    assert fake_playwright.chromium.last_options["no_defaults"] is True
    assert page_url == XIANYU_HOME_URL
    assert len(fake_playwright.chromium.browser.contexts[0].pages) == 2
    assert fake_playwright.chromium.browser.contexts[0].pages[-1].brought_to_front is True
    assert fake_playwright.stopped is True


def test_playwright_driver_uses_no_window_creation_flag_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if manager_module.os.name != "nt":
        pytest.skip("Windows-only process creation behavior")
    observed_options: dict[str, object] = {}

    async def fake_create_subprocess_exec(*_args: object, **kwargs: object) -> object:
        observed_options.update(kwargs)
        return object()

    class StartingPlaywright:
        def start(self) -> FakePlaywright:
            asyncio.run(manager_module.asyncio.create_subprocess_exec("node.exe"))
            return FakePlaywright()

    monkeypatch.setattr(
        manager_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(manager_module, "sync_playwright", StartingPlaywright)

    result = manager_module._start_playwright_without_console()

    assert isinstance(result, FakePlaywright)
    assert (
        int(observed_options["creationflags"])
        & manager_module.subprocess.CREATE_NO_WINDOW
    )


@pytest.mark.parametrize(
    "config",
    [
        BrowserConnectionConfig(host="192.168.1.20"),
        BrowserConnectionConfig(port=0),
        BrowserConnectionConfig(timeout_ms=0),
    ],
)
def test_only_valid_local_cdp_endpoints_are_accepted(config: BrowserConnectionConfig) -> None:
    """A remote debugging port must never be exposed as an app connection target."""
    with pytest.raises(BrowserConnectionError):
        config.validate()

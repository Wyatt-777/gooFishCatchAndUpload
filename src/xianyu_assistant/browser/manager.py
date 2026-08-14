"""Safe CDP connection primitives for user-owned Chrome and Edge instances."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)

XIANYU_HOME_URL = "https://www.goofish.com/"
_LOCAL_CDP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class BrowserConnectionError(RuntimeError):
    """Raised when a user-owned browser cannot safely be reached through CDP."""


@dataclass(frozen=True, slots=True)
class BrowserConnectionConfig:
    """Local CDP endpoint settings supplied by the settings page."""

    port: int = 9222
    host: str = "localhost"
    timeout_ms: int = 15_000

    @property
    def endpoint_url(self) -> str:
        """Return the HTTP endpoint accepted by Playwright's CDP connector."""
        return f"http://{self.host}:{self.port}"

    def validate(self) -> None:
        """Reject unsafe or malformed CDP targets before a network request is made."""
        if self.host.lower() not in _LOCAL_CDP_HOSTS:
            raise BrowserConnectionError("为保护浏览器会话，仅允许连接本机 CDP 地址。")
        if not 1 <= self.port <= 65_535:
            raise BrowserConnectionError("CDP 端口必须在 1 到 65535 之间。")
        if self.timeout_ms <= 0:
            raise BrowserConnectionError("浏览器连接超时必须大于 0。")


class BrowserManager:
    """Attach to a CDP-enabled browser without taking ownership of its process."""

    def __init__(self, config: BrowserConnectionConfig) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether a usable default browser context is attached."""
        return self._browser is not None and self._context is not None

    @property
    def timeout_ms(self) -> int:
        """Expose the bounded operation timeout used by browser adapters."""
        return self._config.timeout_ms

    def connect(self) -> None:
        """Connect to the browser's default context over local CDP."""
        if self.is_connected:
            return

        self._config.validate()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(
                self._config.endpoint_url,
                timeout=self._config.timeout_ms,
                no_defaults=True,
            )
            self._context = self._browser.contexts[0]
        except (IndexError, PlaywrightError, OSError) as error:
            self.disconnect()
            raise BrowserConnectionError(
                "无法连接浏览器。请确认 Chrome/Edge 已使用 "
                f"--remote-debugging-port={self._config.port} 启动，且端口仅监听 127.0.0.1。"
            ) from error

    def open_xianyu(self) -> str:
        """Reuse a browser tab when possible, otherwise open Xianyu in a new one."""
        return self.open_url(XIANYU_HOME_URL).url

    def open_url(
        self,
        url: str,
        *,
        response_handler: Callable[[Response], None] | None = None,
    ) -> Page:
        """Open an app-owned tab without navigating a tab the user already has open."""
        context = self._require_context()
        page = context.new_page()
        if response_handler is not None:
            page.on("response", response_handler)
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._config.timeout_ms,
            )
            # The app created this tab in response to an explicit user action.
            # Bring it forward so a completed prefill is not hidden behind an
            # older publish tab.
            page.bring_to_front()
        except PlaywrightError as error:
            raise BrowserConnectionError(
                "浏览器已连接，但打开目标页面失败。请检查网络后重试。"
            ) from error
        return page

    def disconnect(self) -> None:
        """Release Playwright resources without closing the user's browser process."""
        self._context = None
        self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            finally:
                self._playwright = None

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserConnectionError("浏览器尚未连接。")
        return self._context

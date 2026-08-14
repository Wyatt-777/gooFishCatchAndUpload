"""Launch a local Chrome or Edge instance configured for the app's CDP connection."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from xianyu_assistant.browser.manager import BrowserConnectionConfig


class BrowserLaunchError(RuntimeError):
    """Raised when a local browser cannot be prepared for CDP automation."""


@dataclass(frozen=True, slots=True)
class BrowserLaunchResult:
    """Describe whether a compatible browser was launched or already available."""

    executable: Path | None
    profile_directory: Path | None
    reused_running_browser: bool


class BrowserLauncher:
    """Start a browser in an isolated persistent profile without exposing CDP remotely."""

    def __init__(
        self,
        config: BrowserConnectionConfig,
        *,
        profile_directory: Path | None = None,
    ) -> None:
        self._config = config
        self._profile_directory = profile_directory

    def start(self) -> BrowserLaunchResult:
        """Reuse a live local CDP endpoint or launch Chrome/Edge for the application."""
        self._config.validate()
        if _is_cdp_endpoint_available(self._config):
            return BrowserLaunchResult(None, None, reused_running_browser=True)

        executable = _find_browser_executable()
        profile_directory = self._profile_directory or _default_profile_directory(executable)
        profile_directory.mkdir(parents=True, exist_ok=True)
        arguments = [
            str(executable),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._config.port}",
            f"--user-data-dir={profile_directory}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        try:
            subprocess.Popen(arguments, close_fds=True)
        except OSError as error:
            raise BrowserLaunchError(f"无法启动浏览器：{executable}") from error
        return BrowserLaunchResult(executable, profile_directory, reused_running_browser=False)


def _is_cdp_endpoint_available(config: BrowserConnectionConfig) -> bool:
    """Check only the local endpoint; a failed probe simply means launch is needed."""
    try:
        with urlopen(f"{config.endpoint_url}/json/version", timeout=0.3) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _find_browser_executable() -> Path:
    """Find an explicitly configured browser first, then normal Chrome/Edge installs."""
    configured = os.environ.get("XIANYU_BROWSER_PATH")
    if configured:
        configured_path = Path(configured)
        if configured_path.is_file():
            return configured_path
        raise BrowserLaunchError("XIANYU_BROWSER_PATH 指向的浏览器文件不存在。")

    for executable in _browser_executable_candidates():
        if executable.is_file():
            return executable
    raise BrowserLaunchError(
        "未找到 Chrome 或 Edge。请安装 Chrome/Edge，或通过 XIANYU_BROWSER_PATH 配置浏览器路径。"
    )


def _browser_executable_candidates() -> tuple[Path, ...]:
    """Return common per-machine and per-user Chrome/Edge executable paths."""
    candidates: list[Path] = []
    for command in ("chrome.exe", "chrome", "msedge.exe", "msedge"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))

    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    for root in filter(None, roots):
        base = Path(root)
        candidates.extend(
            (
                base / "Google" / "Chrome" / "Application" / "chrome.exe",
                base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            )
        )

    unique_candidates: list[Path] = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return tuple(unique_candidates)


def _default_profile_directory(executable: Path) -> Path:
    """Keep a persistent login session separate from the user's default browser profile."""
    app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "XianyuAssistant"
    browser_name = "edge" if executable.name.lower().startswith("msedge") else "chrome"
    return app_data / f"{browser_name}-cdp-profile"

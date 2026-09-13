"""Tests for automatic startup of the isolated local CDP browser."""

from pathlib import Path

import pytest

from xianyu_assistant.browser import launcher as launcher_module
from xianyu_assistant.browser.launcher import BrowserLauncher, BrowserLaunchError
from xianyu_assistant.browser.manager import BrowserConnectionConfig


class FakeProcess:
    """Minimal subprocess result for launch-command assertions."""

    pid = 1234


def test_launcher_starts_chrome_with_an_isolated_local_cdp_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    launched_arguments: list[str] = []
    launched_options: dict[str, object] = {}
    monkeypatch.setattr(launcher_module, "_is_cdp_endpoint_available", lambda _config: False)
    monkeypatch.setattr(launcher_module, "_browser_executable_candidates", lambda: (executable,))
    monkeypatch.setattr(
        launcher_module.subprocess,
        "Popen",
        lambda arguments, **kwargs: (
            launched_arguments.extend(arguments)
            or launched_options.update(kwargs)
            or FakeProcess()
        ),
    )

    profile_directory = tmp_path / "chrome-cdp-profile"
    result = BrowserLauncher(
        BrowserConnectionConfig(port=9333),
        profile_directory=profile_directory,
    ).start()

    assert result.executable == executable
    assert result.profile_directory == profile_directory
    assert result.reused_running_browser is False
    assert profile_directory.is_dir()
    assert launched_arguments == [
        str(executable),
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9333",
        f"--user-data-dir={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    assert launched_options["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW


def test_launcher_reuses_an_existing_local_cdp_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher_module, "_is_cdp_endpoint_available", lambda _config: True)
    monkeypatch.setattr(
        launcher_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("已有 CDP 浏览器时不应再次启动浏览器。"),
    )

    result = BrowserLauncher(BrowserConnectionConfig()).start()

    assert result.reused_running_browser is True
    assert result.executable is None
    assert result.profile_directory is None


def test_launcher_reports_when_no_supported_browser_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher_module, "_is_cdp_endpoint_available", lambda _config: False)
    monkeypatch.setattr(launcher_module, "_browser_executable_candidates", lambda: ())

    with pytest.raises(BrowserLaunchError, match="未找到 Chrome 或 Edge"):
        BrowserLauncher(BrowserConnectionConfig()).start()

"""Credential storage with no plaintext fallback."""

from __future__ import annotations

import sys
from typing import Protocol


class CredentialStoreUnavailableError(RuntimeError):
    """Raised when an OS-backed secure credential store cannot be used."""


class CredentialStore(Protocol):
    """Minimal secret-store interface used by the DeepSeek client."""

    @property
    def available(self) -> bool:
        """Whether this store is backed by a usable secure provider."""

    def get(self, key: str) -> str | None:
        """Read one secret without logging or exposing its value."""

    def set(self, key: str, value: str) -> None:
        """Store one secret in the secure provider."""

    def delete(self, key: str) -> None:
        """Remove one secret from the secure provider."""


class MemoryCredentialStore:
    """Explicit test-only store; never used as a production fallback."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        if not value:
            raise ValueError("凭据不能为空。")
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class KeyringCredentialStore:
    """Use the installed keyring backend, commonly Windows Credential Manager."""

    def __init__(self, *, service_name: str = "XianyuAssistant", keyring_module: object | None = None) -> None:
        self._service_name = service_name
        self._keyring = keyring_module
        self._unavailable_reason: str | None = None
        if self._keyring is None:
            try:
                import keyring
            except ImportError:
                self._unavailable_reason = "未安装 keyring 安全凭据依赖。"
            else:
                self._keyring = keyring
                if sys.platform == "win32":
                    try:
                        from keyring.backends.Windows import WinVaultKeyring

                        keyring.set_keyring(WinVaultKeyring())
                    except (ImportError, RuntimeError) as error:
                        self._unavailable_reason = f"Windows 安全凭据后端不可用：{error}"
        self._check_backend()

    @property
    def available(self) -> bool:
        return self._keyring is not None and self._unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def get(self, key: str) -> str | None:
        self._ensure_available()
        try:
            return self._keyring.get_password(self._service_name, key)  # type: ignore[union-attr]
        except Exception as error:
            raise CredentialStoreUnavailableError("读取安全凭据失败。") from error

    def set(self, key: str, value: str) -> None:
        self._ensure_available()
        if not value:
            raise ValueError("凭据不能为空。")
        try:
            self._keyring.set_password(self._service_name, key, value)  # type: ignore[union-attr]
        except Exception as error:
            raise CredentialStoreUnavailableError("写入安全凭据失败。") from error

    def delete(self, key: str) -> None:
        self._ensure_available()
        try:
            self._keyring.delete_password(self._service_name, key)  # type: ignore[union-attr]
        except Exception as error:
            # A missing credential is equivalent to an already-cleared value.
            if "not found" not in str(error).casefold():
                raise CredentialStoreUnavailableError("删除安全凭据失败。") from error

    def _check_backend(self) -> None:
        if self._keyring is None:
            return
        try:
            backend = self._keyring.get_keyring()  # type: ignore[union-attr]
            priority = getattr(backend, "priority", 1)
            if priority <= 0:
                self._unavailable_reason = "当前 keyring 没有可用的安全后端。"
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._unavailable_reason = "无法初始化安全凭据后端。"

    def _ensure_available(self) -> None:
        if not self.available:
            raise CredentialStoreUnavailableError(self._unavailable_reason or "安全凭据后端不可用。")

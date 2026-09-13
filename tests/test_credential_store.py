"""CS-2 tests for secure credential boundaries."""

import pytest

from xianyu_assistant.security.credential_store import (
    CredentialStoreUnavailableError,
    KeyringCredentialStore,
    MemoryCredentialStore,
)


class _FakeBackend:
    priority = 1


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.backend = _FakeBackend()

    def get_keyring(self) -> _FakeBackend:
        return self.backend

    def get_password(self, service: str, key: str) -> str | None:
        return self.values.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.values[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        self.values.pop((service, key), None)


def test_keyring_store_uses_the_secure_provider_without_persisting_plaintext_in_app_data() -> None:
    provider = _FakeKeyring()
    store = KeyringCredentialStore(service_name="test-service", keyring_module=provider)

    assert store.available is True
    store.set("deepseek_api_key", "secret-value")
    assert store.get("deepseek_api_key") == "secret-value"
    assert provider.values == {("test-service", "deepseek_api_key"): "secret-value"}
    store.delete("deepseek_api_key")
    assert store.get("deepseek_api_key") is None


def test_unavailable_keyring_does_not_silently_fall_back() -> None:
    provider = _FakeKeyring()
    provider.backend.priority = 0
    store = KeyringCredentialStore(keyring_module=provider)

    assert store.available is False
    with pytest.raises(CredentialStoreUnavailableError):
        store.set("deepseek_api_key", "secret-value")


def test_memory_store_is_explicitly_available_for_unit_tests_only() -> None:
    store = MemoryCredentialStore()
    store.set("deepseek_api_key", "test-value")

    assert store.available is True
    assert store.get("deepseek_api_key") == "test-value"

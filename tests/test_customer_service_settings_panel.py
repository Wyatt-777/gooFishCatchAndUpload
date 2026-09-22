"""CS-2 smoke tests for the local settings UI."""

from pathlib import Path

from xianyu_assistant.customer_service.current_catalog import (
    FIRST_CONTACT_CATALOG_REPLY,
    FIRST_CONTACT_MESSAGE_SETTING,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import MemoryCredentialStore
from xianyu_assistant.ui.customer_service_settings_panel import CustomerServiceSettingsPanel


def test_settings_panel_loads_defaults_without_network_or_keyring(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())

    assert panel.base_url_input.text() == "https://api.deepseek.com"
    assert panel.text_model_input.text() == "deepseek-chat"
    assert panel.first_contact_message_input.toPlainText() == FIRST_CONTACT_CATALOG_REPLY
    assert panel.product_table.rowCount() == 0
    assert panel.media_table.rowCount() == 0
    assert panel.import_button.isEnabled() is False
    assert panel.import_knowledge_button.isEnabled() is False


def test_settings_panel_saves_and_resets_editable_first_contact_message(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())

    panel.first_contact_message_input.setPlainText("欢迎咨询\n请告诉我需要的型号")
    panel.save_first_contact_message()

    assert repository.get_setting(FIRST_CONTACT_MESSAGE_SETTING) == (
        "欢迎咨询\n请告诉我需要的型号"
    )

    reloaded = CustomerServiceSettingsPanel(
        repository,
        credential_store=MemoryCredentialStore(),
    )
    assert reloaded.first_contact_message_input.toPlainText() == (
        "欢迎咨询\n请告诉我需要的型号"
    )

    reloaded.reset_first_contact_message()

    assert repository.get_setting(FIRST_CONTACT_MESSAGE_SETTING) is None
    assert reloaded.first_contact_message_input.toPlainText() == FIRST_CONTACT_CATALOG_REPLY


def test_settings_panel_rejects_an_empty_first_contact_message(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())
    panel.first_contact_message_input.clear()

    panel.save_first_contact_message()

    assert repository.get_setting(FIRST_CONTACT_MESSAGE_SETTING) is None
    assert "不能为空" in panel.config_status_label.text()

"""CS-2 smoke tests for the local settings UI."""

from pathlib import Path

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
    assert panel.product_table.rowCount() == 0
    assert panel.media_table.rowCount() == 0
    assert panel.import_button.isEnabled() is False
    assert panel.import_knowledge_button.isEnabled() is False

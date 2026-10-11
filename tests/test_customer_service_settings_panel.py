"""CS-2 smoke tests for the local settings UI."""

from pathlib import Path

import pytest

from xianyu_assistant.customer_service.business_replies import BUSINESS_REPLY_SETTING_PREFIX
from xianyu_assistant.customer_service.current_catalog import (
    FIRST_CONTACT_CATALOG_REPLY,
    FIRST_CONTACT_MESSAGE_SETTING,
    PRICE_SILENCE_FOLLOW_UP_SETTING,
    install_current_catalog,
)
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository
from xianyu_assistant.security.credential_store import MemoryCredentialStore
from xianyu_assistant.ui.customer_service_settings_panel import (
    CustomerServiceSettingsPanel,
    ProductKnowledgeDialog,
)


def test_settings_panel_loads_defaults_without_network_or_keyring(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())

    assert panel.base_url_input.text() == "https://api.deepseek.com"
    assert panel.text_model_input.text() == "deepseek-chat"
    assert panel.first_contact_message_input.toPlainText() == "当前商品价格我确认下"
    assert panel.price_silence_follow_up_input.toPlainText() == ""
    assert panel.product_table.rowCount() == 0
    assert panel.media_table.rowCount() == 0
    assert panel.import_button.isEnabled() is False
    assert panel.import_knowledge_button.isEnabled() is False


def test_business_reply_editing_preserves_confirmed_facts(qapp: object, tmp_path: Path) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())
    panel.business_reply_inputs["pickup"].setText("可以在广东普宁自提")
    panel.save_business_replies()
    assert repository.get_setting(BUSINESS_REPLY_SETTING_PREFIX + "pickup") == "可以在广东普宁自提"

    panel.business_reply_inputs["pickup"].setText("可以私下转账后自提")
    panel.save_business_replies()
    assert repository.get_setting(BUSINESS_REPLY_SETTING_PREFIX + "pickup") == "可以在广东普宁自提"

    panel.reset_business_replies()
    assert repository.get_setting(BUSINESS_REPLY_SETTING_PREFIX + "pickup") is None


def test_settings_panel_saves_and_resets_editable_first_contact_message(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    install_current_catalog(repository)
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


def test_product_editor_rejects_invalid_or_inverted_prices(qapp: object) -> None:
    dialog = ProductKnowledgeDialog()
    dialog.product_key_input.setText("new-product")
    dialog.name_input.setText("测试商品")
    dialog.listed_price_input.setText("abc")
    with pytest.raises(ValueError, match="商品标价"):
        dialog.product()

    dialog.listed_price_input.setText("398")
    dialog.minimum_price_input.setText("399")
    with pytest.raises(ValueError, match="不能高于"):
        dialog.product()

    dialog.minimum_price_input.setText("378")
    product = dialog.product()
    assert (product.listed_price, product.minimum_price) == ("398.00", "378.00")


def test_settings_panel_saves_and_disables_price_silence_follow_up(
    qapp: object, tmp_path: Path
) -> None:
    repository = CustomerServiceRepository(tmp_path / "assistant.db")
    repository.initialize()
    panel = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())
    panel.price_silence_follow_up_input.setPlainText("刚才的价格还有疑问吗？")
    panel.save_price_silence_follow_up()

    assert repository.get_setting(PRICE_SILENCE_FOLLOW_UP_SETTING) == "刚才的价格还有疑问吗？"
    reloaded = CustomerServiceSettingsPanel(repository, credential_store=MemoryCredentialStore())
    assert reloaded.price_silence_follow_up_input.toPlainText() == "刚才的价格还有疑问吗？"

    reloaded.clear_price_silence_follow_up()
    assert repository.get_setting(PRICE_SILENCE_FOLLOW_UP_SETTING) is None
    assert reloaded.price_silence_follow_up_input.toPlainText() == ""

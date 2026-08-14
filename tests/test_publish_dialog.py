"""Local review-dialog behaviour for the single-source description flow."""

from xianyu_assistant.domain.models import ProductAttribute, ProductRecord
from xianyu_assistant.ui.publish_dialog import PublishDialog


def _product() -> ProductRecord:
    return ProductRecord(
        id=9,
        task_id=2,
        external_id="external-9",
        title="列表摘要",
        price="¥10",
        description="原始宝贝描述",
        category="原始分类",
        url="https://www.goofish.com/item?id=9",
        image_urls=("https://img.example/one.jpg",),
        image_paths=("C:/missing-image.jpg",),
        attributes=(ProductAttribute("成色", "全新"),),
    )


def test_publish_dialog_emits_one_description_only_draft(qapp: object) -> None:
    dialog = PublishDialog(_product())
    received = []
    dialog.prefill_requested.connect(received.append)
    dialog.price_input.setText("¥ 12.50")
    dialog.description_input.setText("已修改宝贝描述")

    dialog.prefill_button.click()

    assert len(received) == 1
    assert received[0].product_id == 9
    assert received[0].price == "¥ 12.50"
    assert received[0].description == "已修改宝贝描述"
    assert received[0].attributes == (ProductAttribute("成色", "全新"),)
    assert not hasattr(dialog, "title_input")
    assert "将原样写入宝贝描述" in dialog.description_preview_label.text()


def test_publish_dialog_makes_prefill_progress_and_failure_visible(qapp: object) -> None:
    dialog = PublishDialog(_product())

    dialog.set_prefill_in_progress()
    assert dialog.prefill_button.isEnabled() is False
    assert dialog.prefill_status_label.isHidden() is False

    dialog.set_prefill_failure("页面控件已变更")
    assert dialog.prefill_button.isEnabled() is True
    assert "页面控件已变更" in dialog.prefill_status_label.text()

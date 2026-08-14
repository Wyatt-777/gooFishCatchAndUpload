"""Safety and data-flow tests for publish-page prefill."""

from pathlib import Path

import pytest

from xianyu_assistant.domain.models import ProductAttribute
from xianyu_assistant.publishing.xianyu_publisher import (
    PUBLISH_URL,
    PublishDraft,
    PublishPreparationError,
    XianyuPublisher,
)


class FakeLocator:
    """Record the only supported non-submitting form operations."""

    def __init__(
        self,
        count: int = 0,
        *,
        labels: list[str] | None = None,
        indexed_locators: list["FakeLocator"] | None = None,
        metadata: list[dict[str, object]] | None = None,
        nested_locators: dict[str, "FakeLocator"] | None = None,
    ) -> None:
        self._count = count
        self._labels = labels or []
        self._indexed_locators = indexed_locators or []
        self._metadata = metadata
        self._nested_locators = nested_locators or {}
        self.filled: list[str] = []
        self.uploaded_paths: list[str] = []
        self.clicked = 0

    def count(self) -> int:
        return self._count

    def fill(self, value: str, **_kwargs: object) -> None:
        self.filled.append(value)

    def set_input_files(self, paths: list[str], **_kwargs: object) -> None:
        self.uploaded_paths.extend(paths)

    def evaluate_all(self, _expression: str) -> list[object]:
        if self._metadata is not None:
            return self._metadata
        return [label.replace("*", "").strip() for label in self._labels]

    def nth(self, index: int) -> "FakeLocator":
        return self._indexed_locators[index]

    def locator(self, selector: str) -> "FakeLocator":
        return self._nested_locators.get(selector, FakeLocator())

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, **_kwargs: object) -> None:
        return None

    def click(self, **_kwargs: object) -> None:
        self.clicked += 1


class FakeRichTextLocator(FakeLocator):
    """Minimal contenteditable double that preserves the JS-written text."""

    def __init__(self) -> None:
        super().__init__(1)
        self.rendered_text = ""

    def evaluate(self, expression: str, value: str | None = None) -> str | None:
        if "tagName" in expression:
            return "DIV"
        if "replaceChildren" in expression:
            self.rendered_text = value or ""
            return None
        if "innerText" in expression:
            return self.rendered_text
        raise AssertionError(f"Unexpected script: {expression}")


class FakePage:
    """Small page double exposing only the locator calls used by the adapter."""

    url = PUBLISH_URL

    def __init__(self, locators: dict[str, FakeLocator]) -> None:
        self._locators = locators

    def locator(self, selector: str) -> FakeLocator:
        return self._locators.get(selector, FakeLocator())


class FakeBrowserManager:
    """Prevent tests from opening a real browser while tracking resource release."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.connected = False
        self.disconnected = False
        self.opened_url = ""
        self.timeout_ms = 15_000

    def connect(self) -> None:
        self.connected = True

    def open_url(self, url: str) -> FakePage:
        self.opened_url = url
        return self.page

    def disconnect(self) -> None:
        self.disconnected = True


def _locators() -> dict[str, FakeLocator]:
    return {
        "textarea[placeholder*='描述']": FakeLocator(1),
        "input[placeholder*='价格']": FakeLocator(1),
        "input[type='file']": FakeLocator(1),
    }


def _draft(image_path: Path) -> PublishDraft:
    return PublishDraft(
        product_id=7,
        price="¥ 9.90",
        description="测试宝贝描述",
        category="原始分类",
        image_paths=(str(image_path),),
        attributes=(ProductAttribute("成色", "全新"),),
    )


def test_prepare_prefills_description_images_then_price_without_submitting(tmp_path: Path) -> None:
    """The current page receives a single description field, never a guessed title."""
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    locators = _locators()
    browser = FakeBrowserManager(FakePage(locators))

    result = XianyuPublisher(browser).prepare(_draft(image_path))  # type: ignore[arg-type]

    assert browser.connected is True
    assert browser.disconnected is True
    assert browser.opened_url == PUBLISH_URL
    assert locators["textarea[placeholder*='描述']"].filled == ["测试宝贝描述"]
    assert locators["input[placeholder*='价格']"].filled == ["9.90"]
    assert locators["input[type='file']"].uploaded_paths == [str(image_path)]
    assert result.pending_attributes == (ProductAttribute("成色", "全新"),)
    assert not hasattr(XianyuPublisher, "publish")
    assert not hasattr(XianyuPublisher, "submit")


def test_prepare_requires_downloaded_local_images_before_opening_a_publish_page(
    tmp_path: Path,
) -> None:
    browser = FakeBrowserManager(FakePage(_locators()))
    missing_draft = PublishDraft(1, "¥1", "描述", "", (str(tmp_path / "missing.jpg"),))

    with pytest.raises(PublishPreparationError, match="本地图片"):
        XianyuPublisher(browser).prepare(missing_draft)  # type: ignore[arg-type]

    assert browser.connected is False
    assert browser.disconnected is False


def test_prepare_stops_when_a_publish_control_is_not_uniquely_identified(tmp_path: Path) -> None:
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    locators = _locators()
    locators["textarea[placeholder*='描述']"] = FakeLocator(2)
    browser = FakeBrowserManager(FakePage(locators))

    with pytest.raises(PublishPreparationError, match="多个"):
        XianyuPublisher(browser).prepare(_draft(image_path))  # type: ignore[arg-type]

    assert browser.disconnected is True


def test_prepare_resolves_all_controls_before_transmitting_any_field(tmp_path: Path) -> None:
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    locators = _locators()
    del locators["input[placeholder*='价格']"]
    browser = FakeBrowserManager(FakePage(locators))

    with pytest.raises(PublishPreparationError, match="价格"):
        XianyuPublisher(browser).prepare(_draft(image_path))  # type: ignore[arg-type]

    assert locators["textarea[placeholder*='描述']"].filled == []


def test_current_publish_page_targets_selling_price_without_a_title_field(tmp_path: Path) -> None:
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    price = FakeLocator()
    locators = {
        "[contenteditable='true']": FakeLocator(1),
        "input[placeholder='0.00']": FakeLocator(
            2,
            labels=["价格*", "原价"],
            indexed_locators=[price, FakeLocator()],
        ),
        "input[name='file']": FakeLocator(1),
    }

    XianyuPublisher(FakeBrowserManager(FakePage(locators))).prepare(_draft(image_path))  # type: ignore[arg-type]

    assert locators["[contenteditable='true']"].filled == ["测试宝贝描述"]
    assert price.filled == ["9.90"]


def test_prepare_rejects_a_price_range_before_writing_any_field(tmp_path: Path) -> None:
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"image")
    locators = _locators()
    browser = FakeBrowserManager(FakePage(locators))
    draft = PublishDraft(1, "¥25 - 246.8", "描述", "", (str(image_path),))

    with pytest.raises(PublishPreparationError, match="区间价"):
        XianyuPublisher(browser).prepare(draft)  # type: ignore[arg-type]

    assert locators["textarea[placeholder*='描述']"].filled == []


def test_dynamic_attributes_fill_only_exact_options_after_the_page_renders() -> None:
    category = FakeLocator()
    condition = FakeLocator()
    item_type = FakeLocator()
    controls = FakeLocator(
        3,
        indexed_locators=[category, condition, item_type],
        metadata=[
            {"index": 0, "label": "分类", "text": "自行车变速器"},
            {"index": 1, "label": "成色", "text": "请选择成色"},
            {"index": 2, "label": "类型", "text": "前拔"},
        ],
    )
    new_condition = FakeLocator()
    options = FakeLocator(
        4,
        indexed_locators=[FakeLocator(), FakeLocator(), FakeLocator(), new_condition],
        metadata=[
            {"index": 0, "text": "全新"},
            {"index": 1, "text": "几乎全新"},
            {"index": 2, "text": "轻微使用痕迹"},
            {"index": 3, "text": "明显使用痕迹"},
        ],
    )

    class AttributePage:
        def locator(self, selector: str) -> FakeLocator:
            if selector == ".ant-select":
                return controls
            return options

    publisher = XianyuPublisher(FakeBrowserManager(FakePage(_locators())))  # type: ignore[arg-type]
    filled, pending = publisher._fill_attributes(
        AttributePage(),
        (ProductAttribute("成色", "明显使用痕迹"), ProductAttribute("类型", "前拔")),
    )

    assert filled == (ProductAttribute("成色", "明显使用痕迹"), ProductAttribute("类型", "前拔"))
    assert pending == ()
    assert category.clicked == 0
    assert condition.clicked == 1
    assert item_type.clicked == 0
    assert new_condition.clicked == 1


def test_description_fill_preserves_blank_lines_in_a_contenteditable_editor() -> None:
    publisher = XianyuPublisher(FakeBrowserManager(FakePage(_locators())))  # type: ignore[arg-type]
    editor = FakeRichTextLocator()

    publisher._fill_description(editor, "First line\n\nThird line")

    assert editor.rendered_text == "First line\n\nThird line"


def test_searchable_brand_uses_a_unique_exact_alias_match() -> None:
    category = FakeLocator()
    brand_search = FakeLocator(1)
    brand = FakeLocator(nested_locators={"input": brand_search})
    controls = FakeLocator(
        2,
        indexed_locators=[category, brand],
        metadata=[
            {"index": 0, "label": "分类", "text": "电动车电池", "searchable": False},
            {"index": 1, "label": "品牌", "text": "请输入宝贝的品牌", "searchable": True},
        ],
    )
    selected_brand = FakeLocator()
    options = FakeLocator(
        1,
        indexed_locators=[selected_brand],
        metadata=[{"index": 0, "text": "南都"}],
    )

    class AttributePage:
        def locator(self, selector: str) -> FakeLocator:
            if selector == ".ant-select":
                return controls
            return options

    publisher = XianyuPublisher(FakeBrowserManager(FakePage(_locators())))  # type: ignore[arg-type]
    results: list[dict[str, str]] = []
    source_brand = ProductAttribute("品牌", "NARADA/南都")

    filled, pending = publisher._fill_attributes(
        AttributePage(),
        (source_brand,),
        results=results,
    )

    assert filled == (source_brand,)
    assert pending == ()
    assert brand.clicked == 1
    assert brand_search.filled == ["NARADA/南都"]
    assert selected_brand.clicked == 1
    assert results == [
        {
            "source_name": "品牌",
            "source_value": "NARADA/南都",
            "status": "filled",
            "reason": "option_selected",
            "selected_value": "南都",
        }
    ]

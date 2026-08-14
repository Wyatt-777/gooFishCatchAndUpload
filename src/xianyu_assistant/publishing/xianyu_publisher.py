"""Safe Xianyu publish-page prefill adapter.

The adapter deliberately has no submit, confirm, or click method.  It can only
open a publish page, prefill explicitly identified fields and upload local
images.  Category selection and final publishing remain user actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import Error as PlaywrightError

from xianyu_assistant.browser.manager import BrowserConnectionError, BrowserManager
from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.domain.models import ProductAttribute

PUBLISH_URL = "https://www.goofish.com/publish"
_CURRENT_PAGE_DESCRIPTION_LIMIT = 1500
_CONTENTEDITABLE_DESCRIPTION_SCRIPT = r"""
(element, text) => {
  const fragment = document.createDocumentFragment();
  for (const [index, line] of text.split("\n").entries()) {
    if (index > 0) fragment.append(document.createElement("br"));
    fragment.append(document.createTextNode(line));
  }
  element.replaceChildren(fragment);
  element.dispatchEvent(new InputEvent("input", {
    bubbles: true,
    data: text,
    inputType: "insertText",
  }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}
"""


class PublishPreparationError(RuntimeError):
    """Raised when a safe prefill cannot be completed without guessing a control."""


@dataclass(frozen=True, slots=True)
class PublishDraft:
    """User-reviewed values allowed to be prefilled into a new publish page."""

    product_id: int
    price: str
    description: str
    category: str
    image_paths: tuple[str, ...]
    attributes: tuple[ProductAttribute, ...] = ()
    category_path: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishPreparation:
    """A handoff record proving that the flow stops before the final publish action."""

    page_url: str
    category: str
    uploaded_image_count: int
    filled_attributes: tuple[ProductAttribute, ...]
    pending_attributes: tuple[ProductAttribute, ...]
    category_path: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()


class XianyuPublisher:
    """Open and prefill one publish page while preserving final human control."""

    _FIELD_SELECTORS: ClassVar[dict[str, tuple[str, ...]]] = {
        "title": (
            "input[placeholder*='标题']",
            "textarea[placeholder*='标题']",
            "[contenteditable='true'][placeholder*='标题']",
        ),
        "description": (
            "textarea[placeholder*='描述']",
            "[contenteditable='true'][placeholder*='描述']",
        ),
        "price": ("input[placeholder*='价格']",),
        "images": ("input[name='file']", "input[type='file']"),
    }

    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        debug_writer: DebugArtifactWriter | None = None,
    ) -> None:
        self._browser_manager = browser_manager
        self._debug_writer = debug_writer

    def prepare(self, draft: PublishDraft) -> PublishPreparation:
        """Prefill fields and stop before category selection or final submission."""
        image_paths = self._validated_image_paths(draft)
        description_value = _description_value(draft.description)
        price_value = _price_value(draft.price)
        if self._debug_writer is not None:
            self._debug_writer.record_category_publish(
                product_id=draft.product_id,
                category_input=draft.category,
                category_path=draft.category_path,
                category_ids=draft.category_ids,
            )
        try:
            self._browser_manager.connect()
            page = self._browser_manager.open_url(PUBLISH_URL)
            description_input = self._description_locator(page)
            price_input = self._price_locator(page)
            image_input = self._unique_locator(page, "images")
            timeout_ms = self._browser_manager.timeout_ms
            # The current Xianyu form creates its spec controls only after the
            # description, images and price are accepted.  Keep this order explicit.
            self._fill_description(description_input, description_value)
            image_input.set_input_files(image_paths, timeout=timeout_ms)
            price_input.fill(price_value, timeout=timeout_ms)
            attribute_results: list[dict[str, str]] = []
            filled_attributes, pending_attributes = self._fill_attributes(
                page,
                draft.attributes,
                results=attribute_results,
            )
            if self._debug_writer is not None:
                self._debug_writer.record_publish_attributes(
                    product_id=draft.product_id,
                    results=attribute_results,
                )
            return PublishPreparation(
                page_url=page.url,
                category=draft.category,
                uploaded_image_count=len(image_paths),
                filled_attributes=filled_attributes,
                pending_attributes=pending_attributes,
                category_path=draft.category_path,
                category_ids=draft.category_ids,
            )
        except (BrowserConnectionError, PlaywrightError) as error:
            raise PublishPreparationError(str(error)) from error
        finally:
            self._browser_manager.disconnect()

    def _unique_locator(self, page: object, field: str, *, required: bool = True) -> object | None:
        """Return the first uniquely matched version-specific field selector."""
        for selector in self._FIELD_SELECTORS[field]:
            locator = page.locator(selector)  # type: ignore[union-attr]
            count = locator.count()
            if count == 1:
                return locator
            if count > 1:
                raise PublishPreparationError(
                    f"发布页存在多个“{_field_label(field)}”输入控件，已停止预填。"
                )
        if required:
            raise PublishPreparationError(
                f"未识别到唯一的“{_field_label(field)}”输入控件，页面可能已更新。"
            )
        return None

    def _price_locator(self, page: object) -> object:
        """Identify the selling-price input by its visible form label, never by position."""
        legacy_input = self._unique_locator(page, "price", required=False)
        if legacy_input is not None:
            return legacy_input

        inputs = page.locator("input[placeholder='0.00']")  # type: ignore[union-attr]
        try:
            # Waiting on a multi-match locator is strict in Playwright.  Wait
            # for one candidate to render, then use form labels to identify
            # the actual selling-price control below.
            inputs.first.wait_for(state="visible", timeout=self._browser_manager.timeout_ms)
        except PlaywrightError as error:
            raise PublishPreparationError(
                "未识别到唯一的“价格”输入控件，页面可能仍在加载或已更新。"
            ) from error
        if inputs.count() == 0:
            raise PublishPreparationError("未识别到唯一的“价格”输入控件，页面可能已更新。")
        labels = inputs.evaluate_all(
            r"""inputs => inputs.map((input) => {
                let container = input.parentElement;
                while (container) {
                    const label = container.querySelector('.ant-form-item-label');
                    if (label) return (label.textContent || '').replace(/\*/g, '').trim();
                    container = container.parentElement;
                }
                return '';
            })"""
        )
        matched_indexes = [index for index, label in enumerate(labels) if label == "价格"]
        if len(matched_indexes) != 1:
            raise PublishPreparationError("未识别到唯一的“价格”输入控件，页面可能已更新。")
        return inputs.nth(matched_indexes[0])

    def _description_locator(self, page: object) -> object:
        """Wait for the current page's async rich-text editor before validating it."""
        legacy_input = self._unique_locator(page, "description", required=False)
        if legacy_input is not None:
            return legacy_input

        editor = page.locator("[contenteditable='true']")  # type: ignore[union-attr]
        try:
            editor.wait_for(state="visible", timeout=self._browser_manager.timeout_ms)
        except PlaywrightError as error:
            raise PublishPreparationError(
                "未识别到唯一的“宝贝描述”输入控件，页面可能仍在加载或已更新。"
            ) from error
        if editor.count() != 1:
            raise PublishPreparationError("未识别到唯一的“宝贝描述”输入控件，页面可能已更新。")
        return editor

    def _fill_description(self, description_input: object, value: str) -> None:
        """Preserve source line breaks in either a textarea or the live rich-text editor."""
        if not hasattr(description_input, "evaluate"):
            description_input.fill(value, timeout=self._browser_manager.timeout_ms)  # type: ignore[union-attr]
            return
        tag_name = str(description_input.evaluate("element => element.tagName")).upper()  # type: ignore[union-attr]
        if tag_name in {"INPUT", "TEXTAREA"}:
            description_input.fill(value, timeout=self._browser_manager.timeout_ms)  # type: ignore[union-attr]
            return
        description_input.evaluate(_CONTENTEDITABLE_DESCRIPTION_SCRIPT, value)  # type: ignore[union-attr]
        rendered = str(description_input.evaluate("element => element.innerText"))  # type: ignore[union-attr]
        if _normalise_line_endings(rendered) != _normalise_line_endings(value):
            raise PublishPreparationError("发布页未保留宝贝描述换行，已停止预填。")

    def _fill_attributes(
        self,
        page: object,
        attributes: tuple[ProductAttribute, ...],
        *,
        results: list[dict[str, str]] | None = None,
    ) -> tuple[tuple[ProductAttribute, ...], tuple[ProductAttribute, ...]]:
        """Fill dynamically rendered source attributes only when the option is exact."""
        if not attributes:
            return (), ()
        controls = page.locator(".ant-select")  # type: ignore[union-attr]
        try:
            controls.nth(1).wait_for(state="visible", timeout=self._browser_manager.timeout_ms)
        except (IndexError, PlaywrightError):
            _record_attribute_results(results, attributes, "pending", "controls_not_ready")
            return (), attributes
        if controls.count() < 2:
            _record_attribute_results(results, attributes, "pending", "controls_not_ready")
            return (), attributes
        metadata = controls.evaluate_all(
            r"""controls => controls.map((control, index) => {
                let container = control;
                while (container && !container.querySelector('.ant-form-item-label')) {
                    container = container.parentElement;
                }
                const label = container?.querySelector('.ant-form-item-label')?.textContent || '';
                return {
                    index,
                    label: label.replace(/\*/g, '').trim(),
                    text: (control.textContent || '').trim(),
                    searchable: control.classList.contains('ant-select-show-search'),
                };
            })"""
        )
        filled: list[ProductAttribute] = []
        pending: list[ProductAttribute] = []
        for attribute in attributes:
            matches = [
                item
                for item in metadata
                if isinstance(item, dict) and str(item.get("label", "")) == attribute.name
            ]
            if len(matches) != 1:
                pending.append(attribute)
                _record_attribute_result(results, attribute, "pending", "control_not_unique")
                continue
            control_info = matches[0]
            if _normalise_option_text(str(control_info.get("text", ""))) in set(
                _attribute_value_variants(attribute.value)
            ):
                filled.append(attribute)
                _record_attribute_result(results, attribute, "filled", "already_selected")
                continue
            control = controls.nth(int(control_info["index"]))
            control.click(force=True, timeout=self._browser_manager.timeout_ms)
            if bool(control_info.get("searchable")):
                search_input = control.locator("input")
                if search_input.count() != 1:
                    pending.append(attribute)
                    _record_attribute_result(
                        results, attribute, "pending", "search_input_not_unique"
                    )
                    continue
                search_input.fill(attribute.value, timeout=self._browser_manager.timeout_ms)
            options = page.locator(  # type: ignore[union-attr]
                ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option"
            )
            try:
                options.first.wait_for(state="visible", timeout=self._browser_manager.timeout_ms)
            except PlaywrightError:
                pending.append(attribute)
                _record_attribute_result(results, attribute, "pending", "options_not_available")
                continue
            option_metadata = options.evaluate_all(
                "options => options.map((option, index) => ({ index, text: (option.textContent || '').trim() }))"
            )
            accepted_values = set(_attribute_value_variants(attribute.value))
            option_matches = [
                item
                for item in option_metadata
                if isinstance(item, dict)
                and _normalise_option_text(str(item.get("text", ""))) in accepted_values
            ]
            if len(option_matches) != 1:
                pending.append(attribute)
                _record_attribute_result(results, attribute, "pending", "option_not_unique")
                continue
            selected_option = option_matches[0]
            options.nth(int(selected_option["index"])).click(
                force=True,
                timeout=self._browser_manager.timeout_ms,
            )
            filled.append(attribute)
            _record_attribute_result(
                results,
                attribute,
                "filled",
                "option_selected",
                selected_value=str(selected_option.get("text", "")),
            )
        return tuple(filled), tuple(pending)

    @staticmethod
    def _validated_image_paths(draft: PublishDraft) -> list[str]:
        paths = [path for path in draft.image_paths if Path(path).is_file()]
        if not paths:
            raise PublishPreparationError("没有可上传的本地图片，请先下载商品图片后再预填发布页。")
        return paths


def _price_value(value: str) -> str:
    """Turn a displayed price such as ``¥ 9.90`` into a browser input value."""
    normalized = value.replace("¥", "").replace(",", "").strip()
    if not normalized:
        raise PublishPreparationError("价格不能为空。")
    if re.fullmatch(r"\d+(?:\.\d+)?", normalized) is None:
        raise PublishPreparationError(
            "当前商品显示为区间价，请在确认窗口填写一个明确的发布价格后再预填。"
        )
    return normalized


def _description_value(description: str) -> str:
    """Validate the single source field accepted by the current publish page."""
    value = description.strip()
    if not value:
        raise PublishPreparationError("宝贝描述不能为空。")
    if len(value) > _CURRENT_PAGE_DESCRIPTION_LIMIT:
        raise PublishPreparationError(
            f"当前闲鱼页面的宝贝描述最多 1500 字；当前为 {len(value)} 字，请先在确认窗口精简描述。"
        )
    return value


def _normalise_line_endings(value: str) -> str:
    """Compare browser text without treating platform line endings as changes."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _normalise_option_text(value: str) -> str:
    """Normalize display whitespace while keeping the option value meaningful."""
    return " ".join(value.split())


def _attribute_value_variants(value: str) -> tuple[str, ...]:
    """Allow a precise brand alias such as ``NARADA/南都`` to match ``南都``.

    Source attributes often include a Latin brand name and its Chinese trading
    name separated by a slash.  Each individual part is still an exact value;
    the caller must require exactly one matching publish option before use.
    """
    variants: list[str] = []
    for candidate in (value, *re.split(r"[/／]", value)):
        normalised = _normalise_option_text(candidate)
        if normalised and normalised not in variants:
            variants.append(normalised)
    return tuple(variants)


def _record_attribute_result(
    results: list[dict[str, str]] | None,
    attribute: ProductAttribute,
    status: str,
    reason: str,
    *,
    selected_value: str = "",
) -> None:
    """Add one auditable outcome without making the publish flow depend on debug."""
    if results is None:
        return
    result = {
        "source_name": attribute.name,
        "source_value": attribute.value,
        "status": status,
        "reason": reason,
    }
    if selected_value:
        result["selected_value"] = selected_value
    results.append(result)


def _record_attribute_results(
    results: list[dict[str, str]] | None,
    attributes: tuple[ProductAttribute, ...],
    status: str,
    reason: str,
) -> None:
    """Record the same deferred reason for a complete source attribute set."""
    for attribute in attributes:
        _record_attribute_result(results, attribute, status, reason)


def _field_label(field: str) -> str:
    """Translate internal field keys into actionable wording for the user."""
    return {"description": "宝贝描述", "images": "图片"}.get(field, field)

"""HTML adapter for Xianyu search-result cards.

The page uses CSS-module class names, so this adapter relies only on their
stable semantic prefixes and the item URL contract.  The parser also retains
the small ``data-*`` fixture contract used in early development, which makes a
future adapter migration straightforward to test.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

from xianyu_assistant.domain.models import ProductCandidate

_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
_PRODUCTION_CARD_CLASS = "feeds-item-wrap"
_PRODUCTION_FIELD_CLASSES = {
    "row1-wrap-title": "title",
    "row2-wrap-cpv": "description",
    "row3-wrap-price": "price",
}
_DISPLAY_PRICE = re.compile(r"¥\s*(\d+(?:\s*\.\s*\d+)?)")


class XianyuPageStructureError(RuntimeError):
    """Raised when a search page has no result cards matching the maintained contract."""


class XianyuSearchParser(HTMLParser):
    """Extract products from the real Xianyu feed-card structure.

    Real cards are item links whose class begins with ``feeds-item-wrap``.
    Their title, condition preview, price and images live in descendants whose
    semantic CSS-module prefixes are maintained in this module only.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._current: dict[str, object] | None = None
        self._current_kind: str | None = None
        self._field_stack: list[tuple[int, str]] = []
        self._depth = 0
        self._products: list[ProductCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._current is None:
            self._start_product_if_card(tag, attributes)
            return

        if tag not in _VOID_TAGS:
            self._depth += 1
        self._capture_field(tag, attributes)
        self._capture_image(tag, attributes)

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._field_stack:
            return
        field = self._field_stack[-1][1]
        current_value = self._current[field]
        assert isinstance(current_value, str)
        self._current[field] = current_value + data

    def handle_endtag(self, tag: str) -> None:
        if self._current is None or tag in _VOID_TAGS:
            return
        if self._field_stack and self._field_stack[-1][0] == self._depth:
            self._field_stack.pop()
        self._depth -= 1
        if self._depth != 0:
            return
        self._finish_product()

    def products(self) -> list[ProductCandidate]:
        """Return parsed cards, or flag that the page adapter needs an update."""
        if not self._products:
            raise XianyuPageStructureError("未识别到商品卡片，闲鱼页面结构可能已变更。")
        return self._products

    def _start_product_if_card(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag == "article" and attributes.get("data-product-id"):
            self._begin_product(
                kind="fixture",
                external_id=attributes["data-product-id"] or "",
                url="",
            )
            return

        if not self._is_production_card(tag, attributes):
            return
        url = urljoin(self._base_url, attributes.get("href") or "")
        external_id = parse_qs(urlparse(url).query).get("id", [""])[0]
        if not external_id:
            return
        self._begin_product(kind="production", external_id=external_id, url=url)

    def _begin_product(self, *, kind: str, external_id: str, url: str) -> None:
        self._current = {
            "external_id": external_id,
            "title": "",
            "price": "",
            "description": "",
            "category": "",
            "url": url,
            "image_urls": [],
        }
        self._current_kind = kind
        self._field_stack = []
        self._depth = 1

    @staticmethod
    def _is_production_card(tag: str, attributes: dict[str, str | None]) -> bool:
        return (
            tag == "a"
            and _PRODUCTION_CARD_CLASS in (attributes.get("class") or "")
            and "/item" in (attributes.get("href") or "")
        )

    def _capture_field(self, tag: str, attributes: dict[str, str | None]) -> None:
        if self._current is None:
            return
        if self._current_kind == "fixture":
            field = attributes.get("data-field")
            if field in {"title", "price", "description", "category"}:
                self._field_stack.append((self._depth, field))
            if tag == "a" and attributes.get("data-field") == "url":
                self._current["url"] = urljoin(self._base_url, attributes.get("href") or "")
            return

        class_name = attributes.get("class") or ""
        for class_prefix, field in _PRODUCTION_FIELD_CLASSES.items():
            if class_prefix in class_name:
                self._field_stack.append((self._depth, field))
                return

    def _capture_image(self, tag: str, attributes: dict[str, str | None]) -> None:
        if self._current is None or tag != "img":
            return
        if self._current_kind == "fixture" and attributes.get("data-field") != "image":
            return
        source = attributes.get("src") or attributes.get("data-src")
        if not source:
            return
        image_url = urljoin(self._base_url, source)
        images = self._current["image_urls"]
        assert isinstance(images, list)
        if image_url not in images:
            images.append(image_url)

    def _finish_product(self) -> None:
        assert self._current is not None
        image_urls = self._current["image_urls"]
        assert isinstance(image_urls, list)
        price = _compact_text(str(self._current["price"]))
        if self._current_kind == "production":
            price = _first_display_price(price)
        self._products.append(
            ProductCandidate(
                external_id=str(self._current["external_id"]),
                title=_compact_text(str(self._current["title"])),
                price=price,
                description=_compact_text(str(self._current["description"])),
                category=_compact_text(str(self._current["category"])),
                url=str(self._current["url"]),
                image_urls=tuple(image_urls),
            )
        )
        self._current = None
        self._current_kind = None
        self._field_stack = []
        self._depth = 0


def _compact_text(value: str) -> str:
    """Normalize browser line breaks without erasing words separated by markup."""
    return " ".join(value.split())


def _first_display_price(value: str) -> str:
    """Keep the sale price and discard a later struck-through reference price."""
    match = _DISPLAY_PRICE.search(value)
    if match is None:
        return value
    return f"¥{match.group(1).replace(' ', '')}"

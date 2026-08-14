"""Adapter for the live Xianyu item-detail layout.

The listing card is only a discovery surface.  This parser reads the visible
detail panel, where the displayed sale price, free-form item description and
attribute rows actually live.  CSS module suffixes change on deployments, so
selectors use the stable semantic class prefixes observed in the live page.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from xianyu_assistant.domain.models import ProductAttribute, ProductCandidate

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_DESCRIPTION_BREAK = "\ue000"
_DESCRIPTION_BLOCK_TAGS = frozenset({"div", "li", "p"})


class XianyuDetailStructureError(RuntimeError):
    """Raised when a detail page cannot be safely interpreted."""


class XianyuDetailParser(HTMLParser):
    """Turn the already-scoped item-detail panel into the product contract."""

    def __init__(
        self,
        candidate: ProductCandidate,
        price_raw_text: str,
        image_urls: tuple[str, ...],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._candidate = candidate
        self._price_raw_text = price_raw_text
        self._depth = 0
        self._description_depth: int | None = None
        self._attribute_list_depth: int | None = None
        self._attribute_depth: int | None = None
        self._description_seen = False
        self._attribute_list_seen = False
        self._description_parts: list[str] = []
        self._attribute_parts: list[str] = []
        self._attribute_rows: list[str] = []
        self._image_urls = image_urls

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        class_name = attributes.get("class") or ""
        if tag not in _VOID_TAGS:
            self._depth += 1

        if (
            not self._description_seen
            and self._description_depth is None
            and _has_class_prefix(class_name, "main--")
        ):
            self._description_depth = self._depth
            self._description_seen = True
        if (
            not self._attribute_list_seen
            and self._attribute_list_depth is None
            and _has_class_prefix(class_name, "labels--")
        ):
            self._attribute_list_depth = self._depth
            self._attribute_list_seen = True
        if (
            self._attribute_list_depth is not None
            and self._attribute_depth is None
            and _has_class_prefix(class_name, "item--")
        ):
            self._attribute_depth = self._depth
            self._attribute_parts = []

        if tag == "br" and self._description_depth is not None:
            self._description_parts.append(_DESCRIPTION_BREAK)
        elif (
            tag in _DESCRIPTION_BLOCK_TAGS
            and self._description_depth is not None
            and self._depth > self._description_depth
        ):
            self._append_description_break()

    def handle_data(self, data: str) -> None:
        if self._description_depth is not None:
            self._description_parts.append(data)
        if self._attribute_depth is not None:
            self._attribute_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self._attribute_depth == self._depth:
            row = _compact_text("".join(self._attribute_parts))
            if row:
                self._attribute_rows.append(row)
            self._attribute_depth = None
            self._attribute_parts = []
        if self._description_depth == self._depth:
            self._description_depth = None
        elif (
            tag in _DESCRIPTION_BLOCK_TAGS
            and self._description_depth is not None
            and self._depth > self._description_depth
        ):
            self._append_description_break()
        if self._attribute_list_depth == self._depth:
            self._attribute_list_depth = None
        self._depth -= 1

    def _append_description_break(self) -> None:
        if self._description_parts and self._description_parts[-1] == _DESCRIPTION_BREAK:
            return
        self._description_parts.append(_DESCRIPTION_BREAK)

    def product(self) -> ProductCandidate:
        """Return detail-page fields, refusing to silently retain list summaries."""
        price = normalize_detail_price(self._price_raw_text)
        description = _line_preserving_text("".join(self._description_parts))
        if not price:
            raise XianyuDetailStructureError("未识别到详情页的商品售价，页面结构可能已更新。")
        if not description:
            raise XianyuDetailStructureError("未识别到详情页的宝贝描述，页面结构可能已更新。")
        if not self._image_urls:
            raise XianyuDetailStructureError("未识别到详情页左侧主图集，页面结构可能已更新。")
        return ProductCandidate(
            external_id=self._candidate.external_id,
            # The source has no distinct title.  This is a list-only preview,
            # while the full source text is kept in ``description``.
            title=_description_preview(description),
            price=price,
            description=description,
            category=self._candidate.category,
            url=self._candidate.url,
            image_urls=self._image_urls,
            category_path=self._candidate.category_path,
            category_ids=self._candidate.category_ids,
            attributes=tuple(
                _parse_attribute(row) for row in self._attribute_rows if _parse_attribute(row)
            ),
        )


def _compact_text(value: str) -> str:
    return " ".join(value.split())


def _line_preserving_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.split(_DESCRIPTION_BREAK)]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def normalize_detail_price(value: str) -> str:
    """Normalize a verified detail-price node without guessing another number."""
    compact = _compact_text(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", compact)
    if match is None:
        return ""
    start, end = match.groups()
    return f"¥{start}" if end is None else f"¥{start} - {end}"


def _description_preview(description: str) -> str:
    first_line = next((line for line in description.splitlines() if line.strip()), description)
    return first_line[:80]


def _parse_attribute(row: str) -> ProductAttribute | None:
    normalized = row.replace("：", ":")
    if ":" not in normalized:
        return None
    name, value = (part.strip() for part in normalized.split(":", 1))
    return ProductAttribute(name, value) if name and value else None


def _has_class_prefix(class_name: str, prefix: str) -> bool:
    """Match one CSS-module token instead of a coincidental substring."""
    return any(token.startswith(prefix) for token in class_name.split())

"""Browser-backed search collection using the isolated Xianyu page parser."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError

from xianyu_assistant.browser.manager import BrowserConnectionError, BrowserManager
from xianyu_assistant.crawler.xianyu_detail_parser import (
    XianyuDetailParser,
    XianyuDetailStructureError,
    normalize_detail_price,
)
from xianyu_assistant.crawler.xianyu_parser import XianyuPageStructureError, XianyuSearchParser
from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.domain.models import ProductCandidate

logger = logging.getLogger(__name__)

_DETAIL_PRICE_SELECTOR = "[class*='price--']"
_DETAIL_GALLERY_SELECTOR = "[class*='item-main-window--']"
_DETAIL_THUMBNAIL_SELECTOR = (
    "[class*='item-main-window-list--'] [class*='item-main-window-list-item--'] img"
)
_DETAIL_CAROUSEL_IMAGE_SELECTOR = ".slick-slide:not(.slick-cloned) img"
_IMAGE_ASSET_PATTERN = re.compile(r"(?i)^(?P<asset>.+?\.(?:jpe?g|png|webp|gif))(?:_[^/?]+)?$")
_DETAIL_CATEGORY_API = "mtop.taobao.idle.pc.detail"
_DETAIL_CATEGORY_LEVELS = (
    ("rootChannelCatId", "rootChannelCatName"),
    ("level2ChannelCatId", "level2ChannelCatName"),
    ("level3ChannelCatId", "level3ChannelCatName"),
    ("channelCatId", "channelCatName"),
)
_SELLER_PROFILE_PATH = "/personal"
_SELLER_PROFILE_ITEM_SELECTOR = 'a[href*="/item?id="]'
_SELLER_PROFILE_SCROLL_ATTEMPTS = 12
_SELLER_PROFILE_SCROLL_WAIT_MS = 500
_SELLER_PROFILE_LINKS_SCRIPT = """
links => links.map((link) => link.href).filter(Boolean)
"""
_SELLER_PROFILE_SCROLL_SCRIPT = """
window.scrollTo(0, document.documentElement.scrollHeight)
"""
_DETAIL_PRICE_CANDIDATES_SCRIPT = """
elements => {
  const hasClassPrefix = (element, prefix) =>
    Array.from(element.classList).some((className) => className.startsWith(prefix));
  const hasCurrencySibling = (element) =>
    Array.from(element.parentElement?.children ?? []).some(
      (sibling) =>
        sibling !== element &&
        hasClassPrefix(sibling, "symbol--") &&
        (sibling.textContent ?? "").trim().includes("¥"),
    );
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  };
  return elements
    .filter((element) => hasClassPrefix(element, "price--"))
    .map((element) => ({
      class_name: element.className,
      raw_text: element.textContent ?? "",
      visible: isVisible(element),
      has_currency_sibling: hasCurrencySibling(element),
      outer_html: element.outerHTML.slice(0, 500),
    }));
}
"""
_IMAGE_CANDIDATES_SCRIPT = """
elements => {
  const domPath = (element) => {
    const parts = [];
    let current = element;
    while (current && current !== document.body && parts.length < 8) {
      const className = typeof current.className === "string"
        ? current.className.trim().split(/\\s+/).slice(0, 2).join(".")
        : "";
      parts.unshift(`${current.tagName.toLowerCase()}${className ? `.${className}` : ""}`);
      current = current.parentElement;
    }
    return parts.join(" > ");
  };
  return elements.map((element) => {
    const source = element.currentSrc || element.getAttribute("src") || element.getAttribute("data-src") || "";
    return {
      url: source ? new URL(source, document.baseURI).href : "",
      class_name: typeof element.className === "string" ? element.className : "",
      dom_path: domPath(element),
    };
  })
  .filter((candidate) => candidate.url);
}
"""


class XianyuCrawlerError(RuntimeError):
    """A user-facing error raised during search navigation or result extraction."""


class SellerProfileUrlError(ValueError):
    """Raised when a source URL is not an HTTPS Xianyu seller homepage."""


def validate_seller_profile_url(value: str) -> str:
    """Return a safe seller homepage URL or explain the expected source link.

    The workflow deliberately accepts only the canonical web personal-homepage
    form.  This keeps the collection adapter scoped to a seller's published
    items instead of accepting search pages, arbitrary redirects, or item URLs.
    """

    normalized = value.strip()
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in {"goofish.com", "www.goofish.com"}:
        raise SellerProfileUrlError("请输入 https://www.goofish.com/personal?userId=... 形式的卖家主页链接。")
    if parts.path.rstrip("/") != _SELLER_PROFILE_PATH:
        raise SellerProfileUrlError("链接必须是闲鱼卖家个人主页（/personal），不能是搜索页或商品详情页。")
    if not parse_qs(parts.query).get("userId"):
        raise SellerProfileUrlError("卖家主页链接缺少 userId，请从浏览器地址栏复制完整链接。")
    return normalized


@dataclass(frozen=True, slots=True)
class DetailImageCandidate:
    """One image node observed inside the authoritative detail gallery."""

    source: str
    url: str
    class_name: str
    dom_path: str

    def as_debug_record(self) -> dict[str, str]:
        return {
            "source": self.source,
            "url": self.url,
            "class": self.class_name,
            "dom_path": self.dom_path,
        }


@dataclass(frozen=True, slots=True)
class DetailImageSelection:
    """Selected image URLs together with their source-node trace."""

    image_urls: tuple[str, ...]
    candidates: tuple[DetailImageCandidate, ...]


@dataclass(frozen=True, slots=True)
class DetailCategoryData:
    """Named category path when available, plus the stable raw IDs from the detail API."""

    raw_item_category: Mapping[str, Any]
    category_path: tuple[str, ...]
    category_ids: tuple[str, ...]


class DetailCategoryCapture:
    """Capture the detail API response attached before the item page navigates."""

    def __init__(self) -> None:
        self.data = DetailCategoryData({}, (), ())

    def observe(self, response: object) -> None:
        response_url = str(getattr(response, "url", ""))
        if _DETAIL_CATEGORY_API not in response_url:
            return
        try:
            payload = response.json()  # type: ignore[attr-defined]
        except (PlaywrightError, ValueError, TypeError) as error:
            logger.info(
                "category_capture_failed url=%s error=%s", response_url, type(error).__name__
            )
            return
        self.data = _category_data_from_detail_payload(payload)


def _detail_root(page: object) -> object:
    """Return the one authoritative item-detail panel, never a document-wide match."""
    if not hasattr(page, "locator"):
        raise XianyuDetailStructureError("详情页不支持 Playwright DOM 定位。")
    root = page.locator(XianyuCrawler._DETAIL_READY_SELECTOR)  # type: ignore[attr-defined]
    root_count = root.count()
    if root_count != 1:
        raise XianyuDetailStructureError(f"详情主信息区域匹配了 {root_count} 个节点。")
    return root


def _read_visible_sale_price(detail_root: object, product: ProductCandidate) -> str:
    """Read one visible, currency-marked price from the scoped detail panel."""
    price_nodes = detail_root.locator(_DETAIL_PRICE_SELECTOR)  # type: ignore[attr-defined]
    raw_candidates = price_nodes.evaluate_all(_DETAIL_PRICE_CANDIDATES_SCRIPT)
    if not isinstance(raw_candidates, list):
        raise XianyuDetailStructureError("详情页价格候选数据格式异常。")
    return _select_unique_sale_price(product, raw_candidates)


def _read_detail_gallery_images(page: object, product: ProductCandidate) -> DetailImageSelection:
    """Read the item's left-side gallery, never images from a search card or other panels."""
    if not hasattr(page, "locator"):
        raise XianyuDetailStructureError("详情页不支持 Playwright DOM 定位。")
    gallery = page.locator(_DETAIL_GALLERY_SELECTOR)  # type: ignore[attr-defined]
    gallery_count = gallery.count()
    if gallery_count != 1:
        raise XianyuDetailStructureError(f"详情页主图集匹配了 {gallery_count} 个节点。")

    thumbnails = _read_image_candidates(
        gallery.locator(_DETAIL_THUMBNAIL_SELECTOR),
        source="thumbnail",
    )
    carousel = _read_image_candidates(
        gallery.locator(_DETAIL_CAROUSEL_IMAGE_SELECTOR),
        source="carousel",
    )
    return DetailImageSelection(
        image_urls=_select_detail_gallery_images(
            product,
            (candidate.url for candidate in thumbnails),
            (candidate.url for candidate in carousel),
        ),
        candidates=thumbnails + carousel,
    )


def _read_image_candidates(image_nodes: object, *, source: str) -> tuple[DetailImageCandidate, ...]:
    raw_candidates = image_nodes.evaluate_all(_IMAGE_CANDIDATES_SCRIPT)  # type: ignore[attr-defined]
    if not isinstance(raw_candidates, list):
        raise XianyuDetailStructureError("详情页主图 URL 数据格式异常。")
    candidates: list[DetailImageCandidate] = []
    for candidate in raw_candidates:
        if isinstance(candidate, str):
            candidates.append(DetailImageCandidate(source, candidate, "", ""))
            continue
        if not isinstance(candidate, dict):
            continue
        image_url = candidate.get("url")
        if not isinstance(image_url, str) or not image_url:
            continue
        candidates.append(
            DetailImageCandidate(
                source=source,
                url=image_url,
                class_name=str(candidate.get("class_name", "")),
                dom_path=str(candidate.get("dom_path", "")),
            )
        )
    return tuple(candidates)


def _select_detail_gallery_images(
    product: ProductCandidate,
    thumbnail_urls: Iterable[str],
    carousel_urls: Iterable[str],
) -> tuple[str, ...]:
    """Keep thumbnail order while replacing each item with its non-cloned full-size image."""
    thumbnails = tuple(thumbnail_urls)
    carousel = tuple(carousel_urls)
    high_resolution_by_asset: dict[str, str] = {}
    for image_url in carousel:
        high_resolution_by_asset.setdefault(_image_asset_identity(image_url), image_url)

    selected: list[str] = []
    selected_assets: set[str] = set()
    high_resolution_matches = 0
    for thumbnail_url in thumbnails:
        asset = _image_asset_identity(thumbnail_url)
        if asset in selected_assets:
            continue
        selected_assets.add(asset)
        selected_url = high_resolution_by_asset.get(asset, thumbnail_url)
        if selected_url != thumbnail_url:
            high_resolution_matches += 1
        selected.append(selected_url)

    logger.info(
        "image_selection url=%s thumbnail_count=%s carousel_count=%s "
        "high_resolution_matches=%s final_urls=%s",
        product.url,
        len(thumbnails),
        len(carousel),
        high_resolution_matches,
        json.dumps(selected, ensure_ascii=False),
    )
    if not selected:
        raise XianyuDetailStructureError(f"商品 {product.external_id} 未识别到详情页左侧主图集。")
    return tuple(selected)


def _image_asset_identity(image_url: str) -> str:
    """Ignore CDN size/quality suffixes while retaining the underlying image path."""
    path = unquote(urlsplit(image_url).path)
    match = _IMAGE_ASSET_PATTERN.fullmatch(path)
    return match.group("asset") if match else path


def _category_data_from_detail_payload(payload: object) -> DetailCategoryData:
    """Extract a hierarchy only when the response provides authoritative category names."""
    if not isinstance(payload, dict):
        return DetailCategoryData({}, (), ())
    data = payload.get("data")
    item_data = data.get("itemDO") if isinstance(data, dict) else None
    item_category = item_data.get("itemCatDTO") if isinstance(item_data, dict) else None
    if not isinstance(item_category, dict):
        return DetailCategoryData({}, (), ())

    raw_item_category = {
        key: value
        for key, value in item_category.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    category_ids: list[str] = []
    category_names: list[str] = []
    names_available = True
    for id_key, name_key in _DETAIL_CATEGORY_LEVELS:
        category_id = item_category.get(id_key)
        if category_id is None or str(category_id).strip() == "":
            continue
        normalized_id = str(category_id)
        if normalized_id in category_ids:
            continue
        category_ids.append(normalized_id)
        category_name = item_category.get(name_key)
        if not isinstance(category_name, str) or not category_name.strip():
            names_available = False
            continue
        category_names.append(category_name.strip())
    category_path = tuple(category_names) if category_ids and names_available else ()
    return DetailCategoryData(raw_item_category, category_path, tuple(category_ids))


def _select_unique_sale_price(product: ProductCandidate, candidates: Iterable[object]) -> str:
    """Fail closed unless one candidate is visible, numeric, and currency-associated."""
    trace: list[dict[str, str | bool]] = []
    selected: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_text = str(candidate.get("raw_text", ""))
        normalized_price = normalize_detail_price(raw_text)
        visible = bool(candidate.get("visible"))
        has_currency_sibling = bool(candidate.get("has_currency_sibling"))
        trace.append(
            {
                "class_name": str(candidate.get("class_name", "")),
                "raw_text": raw_text,
                "normalized_price": normalized_price,
                "visible": visible,
                "has_currency_sibling": has_currency_sibling,
                "outer_html": str(candidate.get("outer_html", "")),
            }
        )
        if visible and has_currency_sibling and normalized_price:
            selected.append(raw_text)

    logger.info(
        "price_selection url=%s candidate_count=%s candidates=%s",
        product.url,
        len(trace),
        json.dumps(trace, ensure_ascii=False),
    )
    if len(selected) != 1:
        raise XianyuDetailStructureError(
            f"商品 {product.external_id} 未识别到唯一的可见售价（候选 {len(selected)} 个）。"
        )
    return selected[0]


def _seller_item_candidate(profile_url: str, item_url: str) -> ProductCandidate | None:
    """Turn one same-site item link discovered on a profile into a bare candidate."""

    resolved_url = urljoin(profile_url, item_url)
    parts = urlsplit(resolved_url)
    if (parts.hostname or "").lower() not in {"goofish.com", "www.goofish.com"}:
        return None
    if parts.path.rstrip("/") != "/item":
        return None
    external_id = parse_qs(parts.query).get("id", [""])[0].strip()
    if not external_id:
        return None
    return ProductCandidate(
        external_id=external_id,
        title="",
        price="",
        description="",
        category="",
        url=resolved_url,
        image_urls=(),
    )


def _discover_seller_item_urls(page: object, profile_url: str, max_products: int) -> list[str]:
    """Collect profile item links while a bounded scroll reveals lazy-loaded cards."""

    if max_products < 1:
        raise ValueError("商品数量上限必须至少为 1。")
    if not hasattr(page, "locator"):
        raise XianyuPageStructureError("卖家主页不支持 Playwright DOM 定位。")

    item_urls: list[str] = []
    known_urls: set[str] = set()
    unchanged_scrolls = 0
    if hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(_SELLER_PROFILE_SCROLL_WAIT_MS)  # type: ignore[attr-defined]

    for _ in range(_SELLER_PROFILE_SCROLL_ATTEMPTS):
        links = page.locator(_SELLER_PROFILE_ITEM_SELECTOR)  # type: ignore[attr-defined]
        raw_urls = links.evaluate_all(_SELLER_PROFILE_LINKS_SCRIPT)
        if not isinstance(raw_urls, list):
            raise XianyuPageStructureError("卖家主页商品链接数据格式异常。")
        before_count = len(item_urls)
        for raw_url in raw_urls:
            if not isinstance(raw_url, str):
                continue
            candidate = _seller_item_candidate(profile_url, raw_url)
            if candidate is None or candidate.url in known_urls:
                continue
            known_urls.add(candidate.url)
            item_urls.append(candidate.url)
            if len(item_urls) == max_products:
                return item_urls
        if len(item_urls) == before_count:
            unchanged_scrolls += 1
        else:
            unchanged_scrolls = 0
        if unchanged_scrolls >= 2:
            break
        if hasattr(page, "evaluate"):
            page.evaluate(_SELLER_PROFILE_SCROLL_SCRIPT)  # type: ignore[attr-defined]
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(_SELLER_PROFILE_SCROLL_WAIT_MS)  # type: ignore[attr-defined]

    if not item_urls:
        raise XianyuPageStructureError(
            "未从卖家主页识别到商品。请确认已在应用启动的浏览器中登录，且该主页仍有在售商品。"
        )
    return item_urls


class XianyuCrawler:
    """Discover products, then read their authoritative source detail pages.

    Search cards are intentionally used only to discover live item URLs.  They
    are summaries and may include a crossed-out reference price or truncated
    copy, so publishing data always comes from the individual item page.
    """

    SEARCH_URL = "https://www.goofish.com/search?q={keyword}"
    _DETAIL_READY_SELECTOR = "[class*='item-main-info--']"
    _DETAIL_ATTEMPTS = 2
    _DETAIL_TIMEOUT_MS = 30_000

    def __init__(
        self,
        browser_manager: BrowserManager,
        *,
        debug_writer: DebugArtifactWriter | None = None,
    ) -> None:
        self._browser_manager = browser_manager
        self._debug_writer = debug_writer

    def collect(self, keyword: str) -> list[ProductCandidate]:
        """Navigate to a search page and normalize the available result cards."""
        search_url = self.SEARCH_URL.format(keyword=quote(keyword, safe=""))
        page = None
        try:
            self._browser_manager.connect()
            page = self._browser_manager.open_url(search_url)
            if hasattr(page, "wait_for_selector"):
                page.wait_for_selector(
                    'a[class*="feeds-item-wrap"][href*="/item?id="]',
                    timeout=15_000,
                )
            parser = XianyuSearchParser(page.url)
            parser.feed(page.content())
            return [self._collect_detail(product) for product in parser.products()]
        except (
            BrowserConnectionError,
            PlaywrightError,
            XianyuDetailStructureError,
            XianyuPageStructureError,
        ) as error:
            raise XianyuCrawlerError(str(error)) from error
        finally:
            if page is not None and hasattr(page, "close"):
                try:
                    page.close()
                except PlaywrightError:
                    pass
            self._browser_manager.disconnect()

    def collect_seller_profile(
        self,
        seller_profile_url: str,
        *,
        max_products: int = 30,
    ) -> list[ProductCandidate]:
        """Collect a bounded set of products from one seller's personal homepage.

        The profile page is used only as a discovery surface.  Every returned
        item is subsequently opened as a detail page so prices, gallery images,
        attributes, and category IDs come from their authoritative locations.
        """

        profile_url = validate_seller_profile_url(seller_profile_url)
        profile_page = None
        try:
            self._browser_manager.connect()
            profile_page = self._browser_manager.open_url(profile_url)
            item_urls = _discover_seller_item_urls(profile_page, profile_url, max_products)
            products = [
                self._collect_detail(candidate)
                for item_url in item_urls
                if (candidate := _seller_item_candidate(profile_url, item_url)) is not None
            ]
            if not products:
                raise XianyuPageStructureError("卖家主页未提供可用的商品详情链接。")
            return products
        except (
            BrowserConnectionError,
            PlaywrightError,
            SellerProfileUrlError,
            XianyuDetailStructureError,
            XianyuPageStructureError,
        ) as error:
            raise XianyuCrawlerError(str(error)) from error
        finally:
            if profile_page is not None and hasattr(profile_page, "close"):
                try:
                    profile_page.close()
                except PlaywrightError:
                    pass
            self._browser_manager.disconnect()

    def _collect_detail(self, product: ProductCandidate) -> ProductCandidate:
        """Read one detail page, retrying a transient load before failing closed."""
        last_error: Exception | None = None
        for attempt in range(self._DETAIL_ATTEMPTS):
            detail_page = None
            category_capture = DetailCategoryCapture()
            try:
                detail_page = self._browser_manager.open_url(
                    product.url,
                    response_handler=category_capture.observe,
                )
                if hasattr(detail_page, "wait_for_selector"):
                    # CSS animations can leave a loaded item temporarily hidden.
                    # The parser only needs the rendered DOM, not visual focus.
                    detail_page.wait_for_selector(
                        self._DETAIL_READY_SELECTOR,
                        state="attached",
                        timeout=max(self._DETAIL_TIMEOUT_MS, self._browser_manager.timeout_ms),
                    )
                    detail_page.wait_for_selector(
                        _DETAIL_THUMBNAIL_SELECTOR,
                        state="attached",
                        timeout=max(self._DETAIL_TIMEOUT_MS, self._browser_manager.timeout_ms),
                    )
                detail_root = _detail_root(detail_page)
                price_raw_text = _read_visible_sale_price(detail_root, product)
                image_selection = _read_detail_gallery_images(detail_page, product)
                category_data = category_capture.data
                detail_product = replace(
                    product,
                    category=category_data.category_path[-1]
                    if category_data.category_path
                    else product.category,
                    category_path=category_data.category_path,
                    category_ids=category_data.category_ids,
                )
                self._record_debug_artifacts(detail_product, image_selection, category_data)
                detail_html = detail_root.evaluate("element => element.outerHTML")
                parser = XianyuDetailParser(
                    detail_product, price_raw_text, image_selection.image_urls
                )
                parser.feed(str(detail_html))
                return parser.product()
            except (BrowserConnectionError, PlaywrightError, XianyuDetailStructureError) as error:
                last_error = error
                if attempt + 1 == self._DETAIL_ATTEMPTS:
                    break
            finally:
                if detail_page is not None and hasattr(detail_page, "close"):
                    try:
                        detail_page.close()
                    except PlaywrightError:
                        pass
        assert last_error is not None
        raise XianyuDetailStructureError(f"商品 {product.external_id} 详情页读取失败：{last_error}")

    def _record_debug_artifacts(
        self,
        product: ProductCandidate,
        image_selection: DetailImageSelection,
        category_data: DetailCategoryData,
    ) -> None:
        if self._debug_writer is None:
            return
        self._debug_writer.record_images(
            external_id=product.external_id,
            product_url=product.url,
            candidates=(candidate.as_debug_record() for candidate in image_selection.candidates),
            selected_urls=image_selection.image_urls,
        )
        self._debug_writer.record_category_collection(
            external_id=product.external_id,
            product_url=product.url,
            raw_detail_category=category_data.raw_item_category,
            category_path=category_data.category_path,
            category_ids=category_data.category_ids,
        )

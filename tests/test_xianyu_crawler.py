"""Tests for the search-to-detail collection adapter."""

import pytest

from xianyu_assistant.crawler.xianyu_crawler import (
    XianyuCrawler,
    _category_data_from_detail_payload,
    _select_detail_gallery_images,
    _select_unique_sale_price,
)
from xianyu_assistant.crawler.xianyu_detail_parser import XianyuDetailStructureError
from xianyu_assistant.domain.models import ProductCandidate


class FakePage:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self._html = html
        self.closed = False

    def content(self) -> str:
        return self._html

    def locator(self, selector: str) -> "FakeDetailRoot | FakeGalleryRoot":
        if "item-main-window--" in selector:
            return FakeGalleryRoot()
        return FakeDetailRoot(self._html)

    def close(self) -> None:
        self.closed = True


class FakeDetailRoot:
    def __init__(self, html: str) -> None:
        self._html = html

    def count(self) -> int:
        return 1

    def locator(self, _selector: str) -> "FakePriceNodes":
        return FakePriceNodes()

    def evaluate(self, _expression: str) -> str:
        return self._html


class FakePriceNodes:
    def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
        return [
            {
                "class_name": "price--OEWLbcxC",
                "raw_text": "899",
                "visible": True,
                "has_currency_sibling": True,
                "outer_html": '<div class="price--OEWLbcxC">899</div>',
            }
        ]


class FakeGalleryRoot:
    def count(self) -> int:
        return 1

    def locator(self, selector: str) -> "FakeImageNodes":
        if "item-main-window-list-item--" in selector:
            return FakeImageNodes(
                [
                    "https://img.example/product-one.jpg_220x10000Q90.jpg_.webp",
                    "https://img.example/product-two.jpg_220x10000Q90.jpg_.webp",
                ]
            )
        assert ".slick-slide:not(.slick-cloned) img" == selector
        return FakeImageNodes(
            [
                "https://img.example/product-two.jpg_790x10000Q90.jpg_.webp",
                "https://img.example/product-one.jpg_790x10000Q90.jpg_.webp",
            ]
        )


class FakeImageNodes:
    def __init__(self, image_urls: list[str]) -> None:
        self._image_urls = image_urls

    def evaluate_all(self, _expression: str) -> list[str]:
        return self._image_urls


class FakeBrowserManager:
    """Minimal manager double proving detail pages own the authoritative fields."""

    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.opened_urls: list[str] = []

    def connect(self) -> None:
        self.connected = True

    def open_url(self, url: str, **_kwargs: object) -> FakePage:
        self.opened_urls.append(url)
        if "/search" in url:
            return FakePage(
                "https://www.goofish.com/search?q=%E8%BD%AE%E7%BB%84",
                """
                <a class="feeds-item-wrap--rGdH_KoF" href="/item?id=wheel-001">
                  <div class="row1-wrap-title--qIlOySTh">列表摘要</div>
                  <div class="row3-wrap-price--IZmX7M0K">¥100</div>
                </a>
                """,
            )
        return FakePage(
            url,
            """
            <div class="item-main-info--ExVwW2NW">
              <div class="price--OEWLbcxC">899</div>
              <div class="item-main-window-list--od7DK4Fm"><img src="/image.jpg"></div>
              <div class="main--Nu33bWl6">详情商品描述</div>
              <div class="labels--ndhPFgp8"><div class="item--qI9ENIfp">成色：全新</div></div>
            </div>
            """,
        )

    def disconnect(self) -> None:
        self.disconnected = True


def test_crawler_builds_search_url_then_uses_detail_page_fields() -> None:
    browser_manager = FakeBrowserManager()

    products = XianyuCrawler(browser_manager).collect("轮组")  # type: ignore[arg-type]

    assert browser_manager.connected is True
    assert browser_manager.disconnected is True
    assert browser_manager.opened_urls[0].endswith("q=%E8%BD%AE%E7%BB%84")
    assert browser_manager.opened_urls[1] == "https://www.goofish.com/item?id=wheel-001"
    assert products[0].description == "详情商品描述"
    assert products[0].price == "¥899"
    assert products[0].attributes[0].value == "全新"

    assert products[0].image_urls == (
        "https://img.example/product-one.jpg_790x10000Q90.jpg_.webp",
        "https://img.example/product-two.jpg_790x10000Q90.jpg_.webp",
    )


def test_price_selection_ignores_an_earlier_unrelated_price_node() -> None:
    product = ProductCandidate(
        "item-1", "", "", "", "", "https://www.goofish.com/item?id=item-1", ()
    )

    price = _select_unique_sale_price(
        product,
        [
            {
                "class_name": "price--recommendation",
                "raw_text": "2026",
                "visible": True,
                "has_currency_sibling": False,
                "outer_html": '<div class="price--recommendation">2026</div>',
            },
            {
                "class_name": "price--sale",
                "raw_text": "2599",
                "visible": True,
                "has_currency_sibling": True,
                "outer_html": '<div class="price--sale">2599</div>',
            },
        ],
    )

    assert price == "2599"


def test_gallery_selection_keeps_thumbnail_order_and_excludes_other_images() -> None:
    product = ProductCandidate(
        "item-images",
        "",
        "",
        "",
        "",
        "https://www.goofish.com/item?id=item-images",
        ("https://img.example/search-card.jpg",),
    )

    images = _select_detail_gallery_images(
        product,
        [
            "https://img.example/gallery/one.jpg_220x10000Q90.jpg_.webp",
            "https://img.example/gallery/two.jpg_220x10000Q90.jpg_.webp",
            "https://img.example/gallery/one.jpg_220x10000Q90.jpg_.webp",
        ],
        [
            "https://img.example/avatar.jpg_80x80.jpg",
            "https://img.example/gallery/two.jpg_790x10000Q90.jpg_.webp",
            "https://img.example/gallery/one.jpg_790x10000Q90.jpg_.webp",
            "https://img.example/feeds-card.jpg_220x220.jpg",
        ],
    )

    assert images == (
        "https://img.example/gallery/one.jpg_790x10000Q90.jpg_.webp",
        "https://img.example/gallery/two.jpg_790x10000Q90.jpg_.webp",
    )


def test_gallery_selection_matches_main_image_when_the_source_is_heic() -> None:
    """AliCDN converts HEIC to WebP after adding its size suffix."""
    product = ProductCandidate(
        "item-heic-images",
        "",
        "",
        "",
        "",
        "https://www.goofish.com/item?id=item-heic-images",
        (),
    )

    images = _select_detail_gallery_images(
        product,
        ["https://img.example/gallery/one.heic_220x10000Q90.jpg_.webp"],
        ["https://img.example/gallery/one.heic_790x10000Q90.jpg_.webp"],
    )

    assert images == ("https://img.example/gallery/one.heic_790x10000Q90.jpg_.webp",)


def test_gallery_selection_fails_instead_of_falling_back_to_search_card_images() -> None:
    product = ProductCandidate(
        "item-no-images",
        "",
        "",
        "",
        "",
        "https://www.goofish.com/item?id=item-no-images",
        ("https://img.example/search-card.jpg",),
    )

    with pytest.raises(XianyuDetailStructureError, match="主图集"):
        _select_detail_gallery_images(product, [], ["https://img.example/avatar.jpg"])


def test_category_data_keeps_names_and_ids_from_the_detail_api() -> None:
    data = _category_data_from_detail_payload(
        {
            "data": {
                "itemDO": {
                    "itemCatDTO": {
                        "rootChannelCatId": 1,
                        "rootChannelCatName": "交通工具",
                        "level2ChannelCatId": 2,
                        "level2ChannelCatName": "电动车配件",
                        "level3ChannelCatId": 3,
                        "level3ChannelCatName": "电池",
                        "channelCatId": 4,
                        "channelCatName": "锂电池",
                    }
                }
            }
        }
    )

    assert data.category_path == ("交通工具", "电动车配件", "电池", "锂电池")
    assert data.category_ids == ("1", "2", "3", "4")


def test_category_data_refuses_to_turn_raw_ids_into_category_names() -> None:
    data = _category_data_from_detail_payload(
        {"data": {"itemDO": {"itemCatDTO": {"rootChannelCatId": 1, "channelCatId": 4}}}}
    )

    assert data.category_path == ()
    assert data.category_ids == ("1", "4")


def test_price_selection_fails_closed_when_sale_price_is_ambiguous() -> None:
    product = ProductCandidate(
        "item-2", "", "", "", "", "https://www.goofish.com/item?id=item-2", ()
    )

    with pytest.raises(XianyuDetailStructureError, match="唯一"):
        _select_unique_sale_price(
            product,
            [
                {"raw_text": "899", "visible": True, "has_currency_sibling": True},
                {"raw_text": "999", "visible": True, "has_currency_sibling": True},
            ],
        )

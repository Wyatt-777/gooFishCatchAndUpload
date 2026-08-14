"""Tests for the seller-homepage discovery boundary."""

import pytest

from xianyu_assistant.crawler.xianyu_crawler import (
    SellerProfileUrlError,
    XianyuPageStructureError,
    _discover_seller_item_urls,
    validate_seller_profile_url,
)


class _Links:
    def __init__(self, page: "_ProfilePage") -> None:
        self._page = page

    def evaluate_all(self, _script: str) -> list[str]:
        return self._page.rounds[min(self._page.round_index, len(self._page.rounds) - 1)]


class _ProfilePage:
    def __init__(self, rounds: list[list[str]]) -> None:
        self.rounds = rounds
        self.round_index = 0
        self.scroll_count = 0

    def locator(self, _selector: str) -> _Links:
        return _Links(self)

    def evaluate(self, _script: str) -> None:
        self.scroll_count += 1
        self.round_index += 1

    def wait_for_timeout(self, _timeout: int) -> None:
        return None


def test_validate_seller_profile_url_accepts_only_canonical_https_personal_pages() -> None:
    url = "https://www.goofish.com/personal?userId=12345"

    assert validate_seller_profile_url(f"  {url} ") == url

    for invalid in (
        "http://www.goofish.com/personal?userId=12345",
        "https://www.goofish.com/search?q=bike",
        "https://example.com/personal?userId=12345",
        "https://www.goofish.com/personal",
    ):
        with pytest.raises(SellerProfileUrlError):
            validate_seller_profile_url(invalid)


def test_seller_profile_discovery_deduplicates_lazy_loaded_item_links_and_honours_limit() -> None:
    page = _ProfilePage(
        [
            ["/item?id=first", "/item?id=first"],
            ["/item?id=first", "https://www.goofish.com/item?id=second"],
        ]
    )

    urls = _discover_seller_item_urls(
        page,
        "https://www.goofish.com/personal?userId=42",
        max_products=2,
    )

    assert urls == [
        "https://www.goofish.com/item?id=first",
        "https://www.goofish.com/item?id=second",
    ]
    assert page.scroll_count == 1


def test_seller_profile_discovery_never_reports_an_empty_page_as_success() -> None:
    with pytest.raises(XianyuPageStructureError, match="未从卖家主页识别到商品"):
        _discover_seller_item_urls(
            _ProfilePage([[]]),
            "https://www.goofish.com/personal?userId=42",
            max_products=30,
        )

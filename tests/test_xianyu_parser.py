"""Contract tests for the isolated Xianyu result-card parser."""

import pytest

from xianyu_assistant.crawler.xianyu_parser import XianyuPageStructureError, XianyuSearchParser


def test_parser_extracts_normalized_multimage_product_card() -> None:
    """The adapter retains the raw category and all image source URLs."""
    parser = XianyuSearchParser("https://www.goofish.com/search?q=bike")
    parser.feed(
        """
        <article data-product-id="card-123">
          <a data-field="url" href="/item/card-123">查看</a>
          <span data-field="title">碳纤维轮组</span>
          <span data-field="price">¥ 1200</span>
          <span data-field="description">成色很好</span>
          <span data-field="category">自行车配件</span>
          <img data-field="image" src="/images/one.jpg">
          <img data-field="image" data-src="https://img.example/two.jpg">
        </article>
        """
    )

    products = parser.products()

    assert products[0].external_id == "card-123"
    assert products[0].url == "https://www.goofish.com/item/card-123"
    assert products[0].image_urls == (
        "https://www.goofish.com/images/one.jpg",
        "https://img.example/two.jpg",
    )


def test_parser_reports_when_the_page_contract_has_no_product_cards() -> None:
    """Unexpected page markup needs an actionable adapter error rather than silent data loss."""
    parser = XianyuSearchParser("https://www.goofish.com")
    parser.feed("<main>请登录后继续</main>")

    with pytest.raises(XianyuPageStructureError):
        parser.products()


def test_parser_extracts_real_feed_card_and_uses_sale_price() -> None:
    """The real feed card keeps all card images and ignores reference price."""
    parser = XianyuSearchParser("https://www.goofish.com/search?q=water-bottle")
    parser.feed(
        """
        <a class="feeds-item-wrap--rGdH_KoF" href="/item?id=1058020648383&amp;categoryId=126856586">
          <div class="feeds-image-container--biS2HR4Z">
            <img class="feeds-image--TDRC4fV1" src="//img.example/one.webp">
            <img class="feeds-image--TDRC4fV1" data-src="https://img.example/two.webp">
          </div>
          <div class="feeds-content--blv39SOi">
            <div class="row1-wrap-title--qIlOySTh">碳纤维 <em>水壶架</em></div>
            <div class="row2-wrap-cpv--_dKW4c6D">轻微使用痕迹</div>
            <div class="row3-wrap-price--IZmX7M0K">¥ 700 <span>¥799</span></div>
          </div>
        </a>
        """
    )

    products = parser.products()

    assert len(products) == 1
    product = products[0]
    assert product.external_id == "1058020648383"
    assert product.title == "碳纤维 水壶架"
    assert product.price == "¥700"
    assert product.description == "轻微使用痕迹"
    assert product.category == ""
    assert product.url == "https://www.goofish.com/item?id=1058020648383&categoryId=126856586"
    assert product.image_urls == ("https://img.example/one.webp", "https://img.example/two.webp")

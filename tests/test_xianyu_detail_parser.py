"""Contracts for collecting source fields from a live-style item detail page."""

from xianyu_assistant.crawler.xianyu_detail_parser import XianyuDetailParser
from xianyu_assistant.domain.models import ProductAttribute, ProductCandidate


def test_detail_parser_uses_detail_gallery_images_description_and_attributes() -> None:
    candidate = ProductCandidate(
        external_id="1072008965179",
        title="list summary only",
        price="¥999",
        description="summary only",
        category="",
        url="https://www.goofish.com/item?id=1072008965179",
        image_urls=("https://img.example/list-thumb.webp",),
        category_path=("自行车配件", "变速器"),
        category_ids=("100", "200"),
    )
    parser = XianyuDetailParser(
        candidate,
        "2599",
        ("https://img.example/one-full.webp", "https://img.example/two-full.webp"),
    )
    parser.feed(
        """
        <div class="item-main-info--ExVwW2NW">
          <div class="tips--bJdC_yBS"><div class="value--EyQBSInp">
            <div class="symbol--DK_64UaK">¥</div><div class="price--OEWLbcxC">2599</div>
          </div></div>
          <div class="notLoginContainer--hQCDYhxp">
            <div class="main--Nu33bWl6"><span>顺泰ecs电子变速</span><br><span>绍兴地区授权经销商</span><br><span>小套2599</span></div>
            <div class="labels--ndhPFgp8">
              <div class="item--qI9ENIfp"><div class="label--ejJeaTRV">成色</div><div class="symbol--DK_64UaK">：</div><div class="value--EyQBSInp">全新</div></div>
            </div>
          </div>
        </div>
        """
    )

    product = parser.product()

    assert product.price == "¥2599"
    assert product.description == "顺泰ecs电子变速\n绍兴地区授权经销商\n小套2599"
    assert product.title == "顺泰ecs电子变速"
    assert product.image_urls == (
        "https://img.example/one-full.webp",
        "https://img.example/two-full.webp",
    )
    assert product.category_path == ("自行车配件", "变速器")
    assert product.category_ids == ("100", "200")
    assert product.attributes == (ProductAttribute("成色", "全新"),)


def test_detail_parser_preserves_a_visible_price_range_without_guessing() -> None:
    candidate = ProductCandidate("id", "", "", "", "", "https://www.goofish.com/item?id=id", ())
    parser = XianyuDetailParser(candidate, "25 - 246.8", ("https://img.example/one.webp",))
    parser.feed(
        '<div class="item-main-info--x"><div class="price--x">25 - 246.8</div>'
        '<div class="main--x">多规格商品</div></div>'
    )

    assert parser.product().price == "¥25 - 246.8"


def test_detail_parser_preserves_visible_blank_lines_in_the_description() -> None:
    candidate = ProductCandidate("id", "", "", "", "", "https://www.goofish.com/item?id=id", ())
    parser = XianyuDetailParser(candidate, "10", ("https://img.example/one.webp",))
    parser.feed(
        '<div class="item-main-info--x"><div class="main--x">'
        "First line<br><br>Third line"
        "</div></div>"
    )

    assert parser.product().description == "First line\n\nThird line"

"""CS-1 tests for local prompt and audit redaction."""

from xianyu_assistant.customer_service.privacy import PrivacyRedactor


def test_redactor_covers_contact_identity_address_and_long_numbers() -> None:
    redactor = PrivacyRedactor()
    text = (
        "收件人：张三 地址：上海市浦东新区世纪大道100号，手机号 13800138000，"
        "邮箱 zhangsan@example.com，身份证 110101199001011234，"
        "银行卡 6222021234567890123，订单 123456789012345。"
    )

    redacted = redactor.redact(text)

    for sensitive in (
        "张三",
        "上海市浦东新区世纪大道100号",
        "13800138000",
        "zhangsan@example.com",
        "110101199001011234",
        "6222021234567890123",
        "123456789012345",
    ):
        assert sensitive not in redacted
    assert "[收件人_1]" in redacted
    assert "[地址_1]" in redacted
    assert "[手机号_1]" in redacted


def test_repeated_values_keep_the_same_request_local_placeholder() -> None:
    redactor = PrivacyRedactor()

    redacted = redactor.redact("电话 13800138000；请再次联系 13800138000。")

    assert redacted.count("[手机号_1]") == 2

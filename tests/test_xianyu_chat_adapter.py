"""CS-3 read-only adapter tests using a sanitized DOM fixture contract."""

from pathlib import Path

import pytest

from xianyu_assistant.customer_service.browser_adapter import (
    REAL_XIANYU_CHAT_CONTRACT,
    ChatAdapterError,
    ChatPageContract,
    ChatPageSelectors,
    PlaywrightXianyuChatAdapter,
    ReadOnlyAdapterError,
)
from xianyu_assistant.customer_service.models import MessageKind, PageHealthStatus, SendReceipt
from xianyu_assistant.customer_service.protocols import TextSendNotPerformedError


class FakeElement:
    def __init__(
        self,
        attributes: dict[str, str],
        text: str = "",
        children: dict[str, "FakeElement | list[FakeElement]"] | None = None,
        screenshot: bytes = b"fixture-png",
        on_click: object | None = None,
    ) -> None:
        self.attributes = attributes
        self.text = text
        self.children = children or {}
        self.screenshot_bytes = screenshot
        self.clicked = False
        self.value = ""
        self.enabled = True
        self.on_click = on_click
        self.click_error: Exception | None = None
        self.click_error_after: Exception | None = None

    def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str) -> "FakeLocator":
        child = self.children.get(selector)
        if child is None:
            return FakeLocator([])
        return FakeLocator(child if isinstance(child, list) else [child])

    def click(self) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.clicked = True
        if callable(self.on_click):
            self.on_click()
        if self.click_error_after is not None:
            raise self.click_error_after

    def fill(self, value: str, **_kwargs: object) -> None:
        self.value = value

    def input_value(self) -> str:
        return self.value

    def is_enabled(self) -> bool:
        return self.enabled

    def evaluate(self, expression: str, **_kwargs: object) -> object:
        if ".click()" in expression:
            if not self.enabled:
                return False
            self.click()
            return True
        return None

    def screenshot(self, **_kwargs: object) -> bytes:
        return self.screenshot_bytes


class FakeLocator:
    def __init__(self, elements: list[FakeElement]) -> None:
        self.elements = elements

    def all(self) -> list[FakeElement]:
        return self.elements

    def count(self) -> int:
        return len(self.elements)

    def get_attribute(self, name: str) -> str | None:
        return self.elements[0].get_attribute(name) if self.elements else None

    def inner_text(self) -> str:
        return self.elements[0].inner_text() if self.elements else ""

    def click(self, **_kwargs: object) -> None:
        if self.elements and not _kwargs.get("trial"):
            self.elements[0].click()

    def fill(self, value: str, **kwargs: object) -> None:
        if self.elements:
            self.elements[0].fill(value, **kwargs)

    def input_value(self) -> str:
        return self.elements[0].input_value() if self.elements else ""

    def is_enabled(self) -> bool:
        return bool(self.elements and self.elements[0].is_enabled())

    def evaluate(self, expression: str, **kwargs: object) -> object:
        if not self.elements:
            return None
        return self.elements[0].evaluate(expression, **kwargs)

    def locator(self, selector: str) -> "FakeLocator":
        return self.elements[0].locator(selector) if self.elements else FakeLocator([])

    def screenshot(self, **kwargs: object) -> bytes:
        return self.elements[0].screenshot(**kwargs)


class FakePage:
    url = "https://fixture.invalid/chat"

    def __init__(self, *, dual_price_inputs: bool = False) -> None:
        self.conversations = [
            FakeElement(
                {
                    "data-conversation-key": "conversation-1",
                    "data-unread": "true",
                },
                "顾客一",
            ),
            FakeElement(
                {
                    "data-conversation-key": "conversation-old",
                    "data-unread": "false",
                },
                "旧会话",
            ),
        ]
        self.current = FakeElement({"data-conversation-key": "conversation-1"})
        self.messages = [
            FakeElement(
                {"data-message-key": "message-1", "data-direction": "incoming", "data-message-kind": "text"},
                "请问什么时候发货",
                {".message-text": FakeElement({}, "请问什么时候发货")},
            ),
            FakeElement(
                {"data-message-key": "message-2", "data-direction": "outgoing", "data-message-kind": "text"},
                "今天可以发出",
                {".message-text": FakeElement({}, "今天可以发出")},
            ),
            FakeElement(
                {"data-message-key": "message-3", "data-direction": "incoming", "data-message-kind": "image"},
                "图片",
                {".incoming-image": FakeElement({}, "图片", screenshot=b"fixture-image")},
            ),
            FakeElement(
                {"data-message-key": "message-4", "data-direction": "incoming", "data-message-kind": "voice"},
                "语音",
                {
                    ".voice-transcript-button": FakeElement({}, "转文字"),
                    ".voice-transcript": FakeElement({}, "这是转写文本"),
                },
            ),
        ]
        self.product = FakeElement(
            {"data-product-id": "product-1", "data-product-title": "测试商品"}, "测试商品"
        )
        self.editor = FakeElement({})

        def send_current_text() -> None:
            text = self.editor.value
            self.messages.append(
                FakeElement(
                    {"data-direction": "outgoing", "data-message-kind": "text"},
                    text,
                    {".message-text": FakeElement({}, text)},
                )
            )
            self.editor.value = ""

        self.send_button = FakeElement({}, "发 送", on_click=send_current_text)
        self.price_dialog_open = False
        self.price_confirmation_open = False
        self.price_input = FakeElement({"placeholder": "5.00"})
        self.shipping_input = FakeElement({"placeholder": "0.00"})
        self.message_card_price_button = FakeElement({}, "修改价格")

        def open_price_dialog() -> None:
            self.price_dialog_open = True

        def open_price_confirmation() -> None:
            self.price_confirmation_open = True

        def confirm_price_change() -> None:
            self.price_confirmation_open = False
            self.price_dialog_open = False

        self.top_price_button = FakeElement({}, "修改价格", on_click=open_price_dialog)
        self.price_confirm = FakeElement({}, "确定修改", on_click=open_price_confirmation)
        self.price_final_confirm = FakeElement({}, "确定", on_click=confirm_price_change)
        self.price_confirmation_dialog = FakeElement(
            {},
            "您确定要修改价格吗？",
            {".final-confirm": self.price_final_confirm},
        )
        self.price_dialog = FakeElement(
            {},
            "修改宝贝价格",
            {
                ".price-input": (
                    [self.price_input, self.shipping_input]
                    if dual_price_inputs
                    else self.price_input
                ),
                ".price-confirm": self.price_confirm,
            },
        )

    def locator(self, selector: str) -> FakeLocator:
        return {
            ".conversation": FakeLocator(self.conversations),
            ".active": FakeLocator(self.conversations[:1]),
            ".message": FakeLocator(self.messages),
            ".product-card": FakeLocator([self.product]),
            "#fixture-chat": FakeLocator([self.current]),
            ".editor": FakeLocator([self.editor]),
            ".send": FakeLocator([self.send_button]),
            ".top-price": FakeLocator([self.top_price_button]),
            ".dialog": FakeLocator([self.price_dialog] if self.price_dialog_open else []),
            ".confirmation-dialog": FakeLocator(
                [self.price_confirmation_dialog] if self.price_confirmation_open else []
            ),
            ".final-confirm": FakeLocator(
                [self.price_final_confirm] if self.price_confirmation_open else []
            ),
        }.get(selector, FakeLocator([]))

    def wait_for_selector(self, selector: str, **kwargs: object) -> object:
        state = kwargs.get("state")
        if selector == ".dialog":
            if state == "visible" and not self.price_dialog_open:
                raise RuntimeError("dialog not visible")
            if state == "hidden" and self.price_dialog_open:
                raise RuntimeError("dialog still visible")
        if selector == ".confirmation-dialog":
            if state == "visible" and not self.price_confirmation_open:
                raise RuntimeError("confirmation dialog not visible")
            if state == "hidden" and self.price_confirmation_open:
                raise RuntimeError("confirmation dialog still visible")
        if selector == ".final-confirm":
            if state == "visible" and not self.price_confirmation_open:
                raise RuntimeError("final confirm not visible")
            if state == "hidden" and self.price_confirmation_open:
                raise RuntimeError("final confirm still visible")
        return object()


class NoopDiagnostics:
    def record_failure(self, page: object, reason: str) -> None:
        del page, reason


def _adapter(page: FakePage) -> PlaywrightXianyuChatAdapter:
    return PlaywrightXianyuChatAdapter(
        None,
        ChatPageContract(
            chat_url="https://fixture.invalid/chat",
            verified=True,
            selectors=ChatPageSelectors(
                conversation_items=".conversation",
                message_items=".message",
                text_selector=".message-text",
                product_card=".product-card",
                current_conversation_selector="#fixture-chat",
                incoming_image_selector=".incoming-image",
                voice_transcript_button_selector=".voice-transcript-button",
                voice_transcript_selector=".voice-transcript",
                active_conversation_selector=".active",
                message_input_selector=".editor",
                send_button_selector=".send",
                price_change_button_selector=".top-price",
                price_change_dialog_selector=".dialog",
                price_change_input_selector=".price-input",
                price_change_confirm_selector=".price-confirm",
                price_change_confirmation_dialog_selector=".confirmation-dialog",
                price_change_final_confirm_selector=".final-confirm",
            ),
        ),
        page=page,
        diagnostics=NoopDiagnostics(),  # type: ignore[arg-type]
    )


def test_read_only_adapter_reads_changed_conversations_messages_product_and_media() -> None:
    adapter = _adapter(FakePage())

    assert adapter.check_page_health().status is PageHealthStatus.HEALTHY
    conversations = adapter.list_changed_conversations(limit=10)
    assert [conversation.conversation_key for conversation in conversations] == ["conversation-1"]

    adapter.open_conversation("conversation-1")
    snapshot = adapter.read_conversation()
    assert snapshot.conversation_key == "conversation-1"
    assert snapshot.platform_product_id == "product-1"
    assert snapshot.last_incoming_message is not None
    assert snapshot.messages[2].kind is MessageKind.IMAGE
    assert adapter.capture_incoming_image("message-3").content == b"fixture-image"
    assert adapter.request_voice_transcript("message-4") == "这是转写文本"


def test_selected_conversation_is_polled_after_unread_badge_clears() -> None:
    page = FakePage()
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")
    page.conversations[0].attributes["data-unread"] = "false"

    conversations = adapter.list_changed_conversations(limit=10)

    assert [conversation.conversation_key for conversation in conversations] == [
        "conversation-1"
    ]
    assert conversations[0].has_unread is False


def test_page_active_conversation_is_polled_on_first_round_without_unread() -> None:
    page = FakePage()
    page.conversations[0].attributes["data-unread"] = "false"
    adapter = _adapter(page)

    conversations = adapter.list_changed_conversations(limit=10)

    assert [conversation.conversation_key for conversation in conversations] == [
        "conversation-1"
    ]


def test_unmounted_selected_conversation_is_still_polled() -> None:
    page = FakePage()
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")
    page.conversations = [page.conversations[1]]

    conversations = adapter.list_changed_conversations(limit=10)

    assert [conversation.conversation_key for conversation in conversations] == [
        "conversation-1"
    ]


def test_open_conversation_fails_if_active_identity_does_not_switch() -> None:
    page = FakePage()
    adapter = _adapter(page)

    with pytest.raises(ChatAdapterError, match="未确认切换"):
        adapter.open_conversation("conversation-old")


def test_approved_text_send_is_verified_by_a_new_outgoing_message() -> None:
    adapter = _adapter(FakePage())
    adapter.open_conversation("conversation-1")

    receipt = adapter.send_text("今天可以发出")

    assert adapter.verify_outgoing(receipt) is True
    assert adapter.verify_outgoing(receipt) is False


def test_failed_actual_click_with_retained_text_is_safe_for_manual_retry() -> None:
    page = FakePage()
    message_count = len(page.messages)
    page.send_button.click_error = RuntimeError("fixture click interception")
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")

    with pytest.raises(TextSendNotPerformedError, match="草稿可再次人工确认"):
        adapter.send_text("今天可以发出")

    assert page.editor.value == "今天可以发出"
    assert len(page.messages) == message_count


def test_click_exception_after_send_is_resolved_without_a_second_click() -> None:
    page = FakePage()
    page.send_button.click_error_after = RuntimeError("fixture post-click error")
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")

    receipt = adapter.send_text("今天可以发出")

    assert page.send_button.clicked is True
    assert adapter.verify_outgoing(receipt) is True


def test_media_send_remains_disabled() -> None:
    adapter = _adapter(FakePage())

    with pytest.raises(ReadOnlyAdapterError):
        adapter.send_approved_image(Path("C:/not-sent.png"))
    assert adapter.verify_outgoing(SendReceipt("message", "fingerprint")) is False


def test_price_change_uses_only_top_right_control_and_submits_once() -> None:
    page = FakePage()
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")

    receipt = adapter.change_order_price("428")

    assert receipt.previous_price == "5.00"
    assert receipt.applied_price == "428.00"
    assert page.top_price_button.clicked is True
    assert page.message_card_price_button.clicked is False
    assert page.price_input.value == "428.00"
    assert page.price_confirm.clicked is True
    assert page.price_final_confirm.clicked is True
    assert page.price_confirmation_open is False
    assert page.price_dialog_open is False


def test_price_change_sets_shipping_to_zero_in_two_input_dialog() -> None:
    page = FakePage(dual_price_inputs=True)
    adapter = _adapter(page)
    adapter.open_conversation("conversation-1")

    receipt = adapter.change_order_price("398")

    assert receipt.applied_price == "398.00"
    assert page.price_input.value == "398.00"
    assert page.shipping_input.value == "0"
    assert page.price_final_confirm.clicked is True
    assert page.price_dialog_open is False


def test_real_price_change_selector_uses_semantic_header_not_volatile_topbar_hash() -> None:
    selector = REAL_XIANYU_CHAT_CONTRACT.selectors.price_change_button_selector
    confirmation = REAL_XIANYU_CHAT_CONTRACT.selectors.price_change_confirmation_dialog_selector

    assert selector is not None
    assert '[data-spm="head"]' in selector
    assert 'message-topbar--' not in selector
    assert confirmation == 'text=/您确定要修改价格吗[？?]?/'
    assert REAL_XIANYU_CHAT_CONTRACT.selectors.price_change_final_confirm_selector == 'text="确定"'


def test_unverified_contract_fails_closed_before_touching_page() -> None:
    adapter = PlaywrightXianyuChatAdapter(
        None,
        ChatPageContract(
            chat_url="https://fixture.invalid/chat",
            selectors=ChatPageSelectors(conversation_items=".conversation", message_items=".message"),
        ),
        page=FakePage(),
    )

    with pytest.raises(ChatAdapterError, match="尚未通过"):
        adapter.open_dedicated_chat_page()

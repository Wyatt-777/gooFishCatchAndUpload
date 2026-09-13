"""Xianyu customer-service page adapter with human-approved text sending.

Production URL and selectors are intentionally supplied as a verified contract
from a real, authorized test account.  This module contains all DOM access so
future page changes stay isolated from orchestration and policy code.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError

from xianyu_assistant.browser.manager import BrowserManager
from xianyu_assistant.customer_service.diagnostics import ChatAdapterDiagnostics
from xianyu_assistant.customer_service.fingerprints import content_fingerprint
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    ConversationSummary,
    ImagePayload,
    MessageDirection,
    MessageKind,
    PageHealth,
    PageHealthStatus,
    PriceChangeReceipt,
    SendReceipt,
)
from xianyu_assistant.customer_service.price_change import (
    format_money,
    parse_money,
    parse_nonnegative_money,
)
from xianyu_assistant.customer_service.protocols import (
    PriceChangeNotPerformedError,
    TextSendNotPerformedError,
)


class ChatAdapterError(RuntimeError):
    """Raised when the verified read-only page contract cannot be used."""


class ReadOnlyAdapterError(ChatAdapterError):
    """Raised if a caller attempts a CS-3-forbidden send operation."""


class LocatorLike(Protocol):
    def all(self) -> list[LocatorLike]:
        """Return matching child locators."""

    def count(self) -> int:
        """Return the number of matches."""

    def click(self, **kwargs: object) -> None:
        """Click a read-only page control."""

    def fill(self, value: str, **kwargs: object) -> None:
        """Replace an editable field with approved text."""

    def input_value(self) -> str:
        """Read the current value of an editable field."""

    def is_enabled(self) -> bool:
        """Return whether a control can currently be activated."""

    def bounding_box(self) -> dict[str, float] | None:
        """Return the rendered bounds when supported by the browser locator."""

    def scroll_into_view_if_needed(self, **kwargs: object) -> None:
        """Bring a virtualized node into the viewport when supported."""

    def evaluate(self, expression: str, **kwargs: object) -> object:
        """Run a bounded DOM-only operation on the locator when supported."""

    def get_attribute(self, name: str) -> str | None:
        """Read one DOM attribute."""

    def inner_text(self) -> str:
        """Read rendered text."""

    def locator(self, selector: str) -> LocatorLike:
        """Find descendants using a contract-provided selector."""

    def screenshot(self, **kwargs: object) -> bytes:
        """Capture one DOM node in memory."""


class PageLike(Protocol):
    url: str

    def locator(self, selector: str) -> LocatorLike:
        """Find DOM nodes using a contract-provided selector."""

    def screenshot(self, **kwargs: object) -> bytes:
        """Capture the page or a clipped region in memory."""

    def evaluate(self, expression: str, **kwargs: object) -> object:
        """Run a bounded DOM-only operation in the page."""

    def wait_for_selector(self, selector: str, **kwargs: object) -> object:
        """Wait for one asynchronously rendered selector."""

    def wait_for_timeout(self, timeout: float) -> None:
        """Wait briefly for a virtualized page to render its next state."""


@dataclass(frozen=True, slots=True)
class ChatPageSelectors:
    """Selectors captured from one verified page contract or a fixture."""

    conversation_items: str
    message_items: str
    conversation_key_attribute: str = "data-conversation-key"
    message_key_attribute: str = "data-message-key"
    direction_attribute: str = "data-direction"
    incoming_direction_selector: str | None = None
    outgoing_direction_selector: str | None = None
    kind_attribute: str = "data-message-kind"
    image_kind_selector: str | None = None
    voice_kind_selector: str | None = None
    text_selector: str | None = None
    platform_time_attribute: str = "data-platform-time"
    changed_at_attribute: str = "data-changed-at"
    unread_attribute: str = "data-unread"
    unread_selector: str | None = None
    product_card: str | None = None
    product_id_selector: str | None = None
    product_title_selector: str | None = None
    current_conversation_selector: str | None = None
    active_conversation_selector: str | None = None
    current_conversation_key_attribute: str = "data-conversation-key"
    product_id_attribute: str = "data-product-id"
    product_title_attribute: str = "data-product-title"
    incoming_image_selector: str | None = None
    voice_transcript_button_selector: str | None = None
    voice_transcript_menu_selector: str | None = None
    voice_transcript_menu_text: str | None = None
    voice_transcript_selector: str | None = None
    login_required_marker: str | None = None
    captcha_marker: str | None = None
    risk_control_marker: str | None = None
    conversation_scroll_selector: str | None = None
    message_input_selector: str | None = None
    send_button_selector: str | None = None
    price_change_button_selector: str | None = None
    price_change_dialog_selector: str | None = None
    price_change_input_selector: str | None = None
    price_change_confirm_selector: str | None = None
    price_change_confirmation_dialog_selector: str | None = None
    price_change_final_confirm_selector: str | None = None


@dataclass(frozen=True, slots=True)
class ChatPageContract:
    """Verified URL plus selectors; empty/unverified contracts fail closed."""

    chat_url: str
    selectors: ChatPageSelectors
    verified: bool = False

    def validate(self) -> None:
        if not self.verified:
            raise ChatAdapterError("闲鱼客服页面契约尚未通过真实授权页面勘察。")
        if not self.chat_url.startswith("https://"):
            raise ChatAdapterError("客服页面契约必须使用 HTTPS URL。")
        if not self.selectors.conversation_items or not self.selectors.message_items:
            raise ChatAdapterError("客服页面契约缺少会话或消息选择器。")


# Verified against the user-authorized, logged-in Xianyu page on 2026-09-09.
# The hashed class names are kept here, and nowhere in orchestration/UI code,
# so a future live-page re-survey has one isolated replacement point.
REAL_XIANYU_CHAT_CONTRACT = ChatPageContract(
    chat_url="https://www.goofish.com/im",
    verified=True,
    selectors=ChatPageSelectors(
        conversation_items=".conversation-item--JReyg97P",
        message_items=".message-row--pIWaXNhZ",
        incoming_direction_selector=":scope > div > img.avatar--e05bt3Ju",
        outgoing_direction_selector=":scope > div > div > img.avatar--e05bt3Ju",
        text_selector=".message-text--zV88pB7N",
        image_kind_selector=".image-container--JalHqemi .ant-image-img",
        voice_kind_selector=".voice-container--y_LHeff6",
        unread_selector=".ant-badge-count",
        # The list is virtualized and may unmount the active row after it is
        # scrolled. The stable, rendered chat header remains mounted.
        current_conversation_selector='[data-spm="head"]',
        active_conversation_selector=".conversation-item-active--H2KX6Pb4",
        product_card=".container--dgZTBkgv",
        product_id_selector="a",
        product_title_selector=".text2--S10AsTZt",
        incoming_image_selector=".image-container--JalHqemi > img",
        voice_transcript_button_selector=".message-content--kBUbolyy",
        voice_transcript_menu_selector=".ant-dropdown-menu-item",
        voice_transcript_menu_text="转文字",
        voice_transcript_selector=".voice-to-text-container--G78_FFeM",
        login_required_marker="text=立即登录",
        conversation_scroll_selector=".rc-virtual-list-holder",
        message_input_selector='textarea[placeholder*="请输入消息"]',
        send_button_selector='button:has-text("发 送")',
        # Scope this to the semantic product header.  Xianyu periodically
        # changes the CSS hash on the top bar, while ``data-spm="head"`` stays
        # stable.  A second, visually similar button exists inside an order
        # message card and must never be used by automation.
        price_change_button_selector=(
            '[data-spm="head"] [class*="button--"]:has-text("修改价格")'
        ),
        price_change_dialog_selector='[role="dialog"]:has-text("修改宝贝价格")',
        price_change_input_selector='input.ant-input-number-input',
        price_change_confirm_selector='button:has-text("确定修改")',
        price_change_confirmation_dialog_selector=(
            'text=/您确定要修改价格吗[？?]?/'
        ),
        price_change_final_confirm_selector='text="确定"',
    ),
)


class PlaywrightXianyuChatAdapter:
    """Operate on one dedicated page; only approved text may be sent."""

    def __init__(
        self,
        browser_manager: BrowserManager | None,
        contract: ChatPageContract,
        *,
        page: PageLike | None = None,
        diagnostics: ChatAdapterDiagnostics | None = None,
    ) -> None:
        self._browser_manager = browser_manager
        self._contract = contract
        self._page = page
        self._diagnostics = diagnostics or ChatAdapterDiagnostics()
        self._selected_conversation_key: str | None = None
        self._selected_session_info: dict[str, object] | None = None
        self._conversation_scroll_positions: dict[str, float] = {}
        self._send_baselines: dict[str, set[str]] = {}

    def open_dedicated_chat_page(self) -> None:
        """Create one application-owned page; never navigate an existing page."""
        self._contract.validate()
        if self._page is not None:
            return
        if self._browser_manager is None:
            raise ChatAdapterError("缺少浏览器管理器，无法创建专用客服页。")
        try:
            self._page = self._browser_manager.open_url(self._contract.chat_url)
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            self._diagnostics.record_failure(None, "创建专用客服页失败")
            raise ChatAdapterError("无法创建专用客服页。") from error

    def check_page_health(self) -> PageHealth:
        """Detect login, captcha, risk-control, and structural failures."""
        try:
            self._contract.validate()
            page = self._require_page()
            selectors = self._contract.selectors
            for status, marker in (
                (PageHealthStatus.LOGIN_REQUIRED, selectors.login_required_marker),
                (PageHealthStatus.CAPTCHA, selectors.captcha_marker),
                (PageHealthStatus.RISK_CONTROL, selectors.risk_control_marker),
            ):
                if marker and page.locator(marker).count() > 0:
                    return PageHealth(status, "页面出现安全状态标记。")
            conversation_locator = page.locator(selectors.conversation_items)
            if conversation_locator.count() == 0:
                # Xianyu mounts the conversation list asynchronously after
                # DOMContentLoaded. An immediate count of zero is therefore
                # not evidence of a changed contract. Wait once, boundedly,
                # before failing closed.
                wait_for_selector = getattr(page, "wait_for_selector", None)
                if callable(wait_for_selector):
                    try:
                        wait_for_selector(
                            selectors.conversation_items,
                            state="attached",
                            timeout=10_000,
                        )
                    except PlaywrightError:
                        pass
            if page.locator(selectors.conversation_items).count() == 0:
                return PageHealth(PageHealthStatus.STRUCTURE_CHANGED, "会话列表结构异常。")
            return PageHealth(PageHealthStatus.HEALTHY)
        except ChatAdapterError as error:
            return PageHealth(PageHealthStatus.STRUCTURE_CHANGED, str(error))
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError):
            self._diagnostics.record_failure(self._page, "客服页面健康检查异常")
            return PageHealth(PageHealthStatus.STRUCTURE_CHANGED, "客服页面结构或访问异常。")

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        """List changed/unread entries plus the currently open conversation.

        Xianyu clears the unread badge immediately while a conversation is
        open.  Polling the selected conversation lets the worker's stable
        message fingerprint detect follow-up messages without depending on
        an unread marker that no longer exists.
        """
        if limit <= 0:
            return []
        page = self._require_ready_page()
        selectors = self._contract.selectors
        conversations: list[ConversationSummary] = []
        selected: ConversationSummary | None = None
        selected_key = self._selected_conversation_key
        if selected_key is None and selectors.active_conversation_selector:
            active = page.locator(selectors.active_conversation_selector)
            if active.count() == 1:
                selected_key = self._conversation_key(active, 0)
                self._selected_conversation_key = selected_key
                self._selected_session_info = self._session_info(active)
        for index, item in enumerate(page.locator(selectors.conversation_items).all()):
            unread = (item.get_attribute(selectors.unread_attribute) or "").casefold() in {
                "1",
                "true",
                "yes",
            }
            if selectors.unread_selector and item.locator(selectors.unread_selector).count() > 0:
                unread = True
            changed = item.get_attribute(selectors.changed_at_attribute)
            key = self._conversation_key(item, index)
            is_selected = key == selected_key
            if not unread and not changed and not is_selected:
                continue
            summary = self._summary_from_item(
                item,
                index,
                key=key,
                unread=unread,
                changed=changed,
            )
            if is_selected:
                selected = summary
            else:
                conversations.append(summary)

        # A virtualized list can unmount its selected row even though the
        # conversation header and messages remain rendered and readable.
        if selected is None and selected_key is not None:
            selected = self._summary_from_row(
                dict(self._selected_session_info or {}),
                selected_key,
            )
        ordered = ([selected] if selected is not None else []) + conversations
        return ordered[:limit]

    def list_all_conversations(self) -> list[ConversationSummary]:
        """Read every conversation from the virtualized list without sending anything."""
        page = self._require_ready_page()
        selectors = self._contract.selectors
        holder_selector = selectors.conversation_scroll_selector
        if not holder_selector:
            return [
                self._summary_from_item(item, index, key=self._conversation_key(item, index))
                for index, item in enumerate(page.locator(selectors.conversation_items).all())
            ]

        metrics = page.evaluate(
            """selector => {
                const holder = document.querySelector(selector);
                if (!holder) return null;
                return {scrollTop: holder.scrollTop, scrollHeight: holder.scrollHeight,
                        clientHeight: holder.clientHeight};
            }""",
            holder_selector,
        )
        if not isinstance(metrics, dict):
            raise ChatAdapterError("未找到闲鱼会话滚动容器。")
        original_top = _number(metrics.get("scrollTop"), 0.0)
        seen: dict[str, ConversationSummary] = {}
        position = 0.0
        iterations = 0
        bottom_idle_rounds = 0
        try:
            while iterations < 1_000:
                iterations += 1
                page.evaluate(
                    """({selector, top}) => {
                        const holder = document.querySelector(selector);
                        if (holder) {
                            holder.scrollTo(0, top);
                            holder.dispatchEvent(new Event('scroll', {bubbles: true}));
                        }
                    }""",
                    {"selector": holder_selector, "top": position},
                )
                wait_for_timeout = getattr(page, "wait_for_timeout", None)
                if callable(wait_for_timeout):
                    wait_for_timeout(500 if bottom_idle_rounds else 150)
                raw_rows = self._visible_session_rows(page)
                for index, raw_row in enumerate(raw_rows):
                    key = str(raw_row.get("key") or _fallback_conversation_key(raw_row, index))
                    self._conversation_scroll_positions.setdefault(key, position)
                    seen.setdefault(key, self._summary_from_row(raw_row, key))

                current = page.evaluate(
                    """selector => {
                        const holder = document.querySelector(selector);
                        if (!holder) return null;
                        return {scrollTop: holder.scrollTop, scrollHeight: holder.scrollHeight,
                                clientHeight: holder.clientHeight};
                    }""",
                    holder_selector,
                )
                if not isinstance(current, dict):
                    break
                scroll_top = _number(current.get("scrollTop"), position)
                scroll_height = _number(current.get("scrollHeight"), position)
                client_height = max(_number(current.get("clientHeight"), 0.0), 1.0)
                bottom = max(scroll_height - client_height, 0.0)
                if scroll_top >= bottom - 2:
                    bottom_idle_rounds += 1
                    if bottom_idle_rounds >= 4:
                        break
                    position = scroll_top
                    continue
                bottom_idle_rounds = 0
                next_position = min(position + max(client_height * 0.75, 100.0), bottom)
                if next_position <= position + 1:
                    break
                position = next_position
        finally:
            page.evaluate(
                """({selector, top}) => {
                    const holder = document.querySelector(selector);
                    if (holder) {
                        holder.scrollTo(0, top);
                        holder.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }
                }""",
                {"selector": holder_selector, "top": original_top},
            )
        return list(seen.values())

    def open_conversation(self, conversation_key: str) -> None:
        """Select a conversation item without sending or editing content."""
        page = self._require_ready_page()
        selectors = self._contract.selectors
        if self._try_select_visible_conversation(page, conversation_key):
            return
        for index, item in enumerate(page.locator(selectors.conversation_items).all()):
            if self._conversation_key(item, index) == conversation_key:
                self._select_conversation(page, item, conversation_key)
                return
        holder_selector = selectors.conversation_scroll_selector
        if holder_selector:
            metrics = page.evaluate(
                """selector => {
                    const holder = document.querySelector(selector);
                    if (!holder) return null;
                    return {scrollHeight: holder.scrollHeight, clientHeight: holder.clientHeight};
                }""",
                holder_selector,
            )
            if isinstance(metrics, dict):
                client_height = max(_number(metrics.get("clientHeight"), 1.0), 1.0)
                bottom = max(_number(metrics.get("scrollHeight"), 0.0) - client_height, 0.0)
                known_position = self._conversation_scroll_positions.get(conversation_key)
                if known_position is not None:
                    page.evaluate(
                        """({selector, top}) => {
                            const holder = document.querySelector(selector);
                            if (holder) {
                                holder.scrollTo(0, top);
                                holder.dispatchEvent(new Event('scroll', {bubbles: true}));
                            }
                        }""",
                        {"selector": holder_selector, "top": known_position},
                    )
                    wait_for_timeout = getattr(page, "wait_for_timeout", None)
                    if callable(wait_for_timeout):
                        wait_for_timeout(250)
                    if self._try_select_visible_conversation(page, conversation_key):
                        return
                    for index, item in enumerate(page.locator(selectors.conversation_items).all()):
                        if self._conversation_key(item, index) == conversation_key:
                            self._select_conversation(page, item, conversation_key)
                            return
                position = 0.0
                for _ in range(1_000):
                    page.evaluate(
                        """({selector, top}) => {
                            const holder = document.querySelector(selector);
                            if (holder) {
                                holder.scrollTo(0, top);
                                holder.dispatchEvent(new Event('scroll', {bubbles: true}));
                            }
                        }""",
                        {"selector": holder_selector, "top": position},
                    )
                    wait_for_timeout = getattr(page, "wait_for_timeout", None)
                    if callable(wait_for_timeout):
                        wait_for_timeout(250)
                    for index, item in enumerate(page.locator(selectors.conversation_items).all()):
                        if self._conversation_key(item, index) == conversation_key:
                            self._conversation_scroll_positions[conversation_key] = position
                            self._select_conversation(page, item, conversation_key)
                            return
                    if position >= bottom - 2:
                        break
                    next_position = min(position + max(client_height * 0.75, 100.0), bottom)
                    if next_position <= position + 1:
                        break
                    position = next_position
        # A virtualized list can temporarily unmount the selected row. If the
        # caller is reopening the already selected conversation, the rendered
        # chat header is sufficient to prove that no switch is needed.
        try:
            if self._current_conversation_key(page) == conversation_key:
                self._selected_conversation_key = conversation_key
                return
        except ChatAdapterError:
            pass
        raise ChatAdapterError("未找到指定会话。")

    def _select_conversation(
        self, page: PageLike, item: LocatorLike, conversation_key: str
    ) -> None:
        self._selected_conversation_key = conversation_key
        self._selected_session_info = self._session_info(item)
        try:
            item.click(force=True, no_wait_after=True)
        except TypeError:
            # Keep the fake/test locator contract intentionally small.
            item.click()
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(250)

    def _try_select_visible_conversation(self, page: PageLike, conversation_key: str) -> bool:
        """Match React-backed rows in one DOM evaluation before using dynamic locators."""
        locator = page.locator(self._contract.selectors.conversation_items)
        evaluate_all = getattr(locator, "evaluate_all", None)
        nth = getattr(locator, "nth", None)
        if not callable(evaluate_all) or not callable(nth):
            return False
        try:
            rows = evaluate_all(
                """elements => elements.map((element, index) => {
                    const attribute = element.getAttribute('data-conversation-key');
                    const fiberKey = Object.keys(element).find(key => key.startsWith('__reactFiber'));
                    let fiber = fiberKey ? element[fiberKey] : null;
                    while (fiber && !(fiber.memoizedProps && fiber.memoizedProps.sessionInfo)) {
                        fiber = fiber.return;
                    }
                    const sessionId = fiber?.memoizedProps?.sessionInfo?.sessionId;
                    return {index, key: attribute || (sessionId ? `session-${sessionId}` : null)};
                })"""
            )
        except (PlaywrightError, RuntimeError, TypeError, ValueError):
            return False
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict) or row.get("key") != conversation_key:
                continue
            index = row.get("index")
            if not isinstance(index, int):
                return False
            self._select_conversation(page, nth(index), conversation_key)
            return True
        return False

    def read_conversation(self) -> ConversationSnapshot:
        """Read the selected conversation and optional product card metadata."""
        page = self._require_ready_page()
        selectors = self._contract.selectors
        messages: list[ChatMessage] = []
        for index, item in enumerate(page.locator(selectors.message_items).all()):
            direction = _direction(item, selectors)
            kind = _kind(item, selectors)
            text = _item_text(item, selectors.text_selector)
            message_key = self._message_key(item, index, direction, kind, text)
            messages.append(
                ChatMessage(
                    message_key=message_key,
                    direction=direction,
                    kind=kind,
                    text=text or None,
                    platform_time=_parse_datetime(item.get_attribute(selectors.platform_time_attribute)),
                )
            )
        product_id, product_title = self._read_product_card(page)
        if not product_id or not product_title:
            info = self._selected_session_info or self._session_info_from_page(page)
            if info:
                item_info = info.get("item_info")
                if isinstance(item_info, dict):
                    product_id = product_id or _string(item_info.get("item_id"))
                    product_title = product_title or _string(item_info.get("title"))
        conversation_key = self._current_conversation_key(page)
        return ConversationSnapshot(
            conversation_key=conversation_key,
            messages=tuple(messages),
            platform_product_id=product_id,
            product_title=product_title,
        )

    def request_voice_transcript(self, message_key: str) -> str | None:
        """Click the page's transcript control and read its rendered result."""
        page = self._require_ready_page()
        item = self._find_message(page, message_key)
        selectors = self._contract.selectors
        if not selectors.voice_transcript_selector:
            return None
        if selectors.voice_transcript_button_selector:
            button = item.locator(selectors.voice_transcript_button_selector)
            if button.count() > 0:
                if selectors.voice_transcript_menu_selector:
                    try:
                        button.click(button="right", force=True, no_wait_after=True)
                    except TypeError:
                        button.click()
                    menu_items = page.locator(selectors.voice_transcript_menu_selector).all()
                    for menu_item in menu_items:
                        if (
                            selectors.voice_transcript_menu_text is None
                            or menu_item.inner_text().strip() == selectors.voice_transcript_menu_text
                        ):
                            try:
                                menu_item.click(force=True, no_wait_after=True)
                            except TypeError:
                                menu_item.click()
                            break
                else:
                    button.click()
        transcript = item.locator(selectors.voice_transcript_selector)
        return transcript.inner_text().strip() if transcript.count() else None

    def capture_incoming_image(self, message_key: str) -> ImagePayload:
        """Screenshot one incoming image node into memory for later model use."""
        page = self._require_ready_page()
        item = self._find_message(page, message_key)
        selector = self._contract.selectors.incoming_image_selector
        if not selector:
            raise ChatAdapterError("客服页面契约没有顾客图片选择器。")
        image = item.locator(selector)
        if image.count() != 1:
            raise ChatAdapterError("顾客图片节点不是唯一的。")
        # The live page keeps a hidden high-resolution Ant Image sibling whose
        # locator screenshot waits indefinitely while its preview animation is
        # active. A clipped page screenshot captures the visible node without
        # touching the image URL or uploading anything.
        if hasattr(image, "bounding_box") and hasattr(page, "screenshot"):
            box: object | None = None
            if hasattr(image, "evaluate") and hasattr(page, "evaluate"):
                box = image.evaluate(
                    """element => {
                        element.scrollIntoView({block: 'center', inline: 'nearest'});
                        const rect = element.getBoundingClientRect();
                        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                    }"""
                )
                viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
                if isinstance(box, dict) and isinstance(viewport, dict):
                    box = _clamp_clip(box, viewport)
            elif hasattr(image, "scroll_into_view_if_needed"):
                image.scroll_into_view_if_needed(timeout=5_000)
                box = image.bounding_box()
            else:
                box = image.bounding_box()
            if box is None:
                raise ChatAdapterError("顾客图片节点当前不可见。")
            try:
                content = page.screenshot(type="png", clip=box, animations="disabled", timeout=5_000)
            except PlaywrightError as error:
                raise ChatAdapterError("顾客图片截图失败。") from error
        else:
            content = image.screenshot(type="png")
        return ImagePayload(message_key=message_key, content=content, mime_type="image/png")

    def send_text(self, text: str) -> SendReceipt:
        """Send one approved text once, resolving an ambiguous click by readback."""
        approved_text = text.strip()
        if not approved_text:
            raise ChatAdapterError("发送文本不能为空。")
        page = self._require_ready_page()
        if not self._selected_conversation_key:
            raise ChatAdapterError("发送前没有唯一选中的会话。")
        selectors = self._contract.selectors
        if not selectors.message_input_selector or not selectors.send_button_selector:
            raise ReadOnlyAdapterError("客服页面发送契约尚未配置。")
        editor = page.locator(selectors.message_input_selector)
        button = page.locator(selectors.send_button_selector)
        if editor.count() != 1 or button.count() != 1:
            raise ChatAdapterError("消息输入框或发送按钮不是唯一节点。")

        baseline = {message.message_key for message in self.read_conversation().messages}
        fingerprint = content_fingerprint(approved_text)
        try:
            editor.fill(approved_text, timeout=5_000)
            if editor.input_value().strip() != approved_text:
                raise ChatAdapterError("消息输入框内容校验失败，未发送。")
            if not button.is_enabled():
                raise ChatAdapterError("发送按钮当前不可用，未发送。")
        except ChatAdapterError:
            raise
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            self._record_send_error(page, "输入与按钮状态校验", error)
            raise TextSendNotPerformedError(
                f"发送前页面校验失败（{type(error).__name__}），消息未发送，可再次人工确认。"
            ) from error

        receipt = SendReceipt(f"pending-{fingerprint[:20]}", fingerprint)
        self._send_baselines[fingerprint] = baseline
        try:
            # DOM-native click does not wait for foreground animation frames,
            # which Chrome throttles while the assistant window covers the tab.
            # This remains the single irreversible page action: never retry it.
            clicked = button.evaluate(
                """element => {
                    if (!element.isConnected || element.disabled) return false;
                    element.click();
                    return true;
                }"""
            )
            if clicked is not True:
                raise RuntimeError("send button was detached or disabled")
            return receipt
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            # A click exception does not prove that the browser ignored the
            # action. Read back first so a successful send is never repeated.
            try:
                if self.verify_outgoing(receipt):
                    # The worker performs its own mandatory verification. Keep
                    # the original baseline so that second check also succeeds.
                    self._send_baselines[fingerprint] = baseline
                    return receipt
            except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError):
                pass
            try:
                text_retained = editor.input_value().strip() == approved_text
            except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError):
                text_retained = False
            self._record_send_error(
                page,
                f"实际点击；输入框保留原文={text_retained}",
                error,
            )
            if text_retained:
                raise TextSendNotPerformedError(
                    f"发送按钮点击未生效（{type(error).__name__}），已确认消息未出现，"
                    "草稿可再次人工确认。"
                ) from error
            raise ChatAdapterError(
                f"发送按钮点击异常（{type(error).__name__}），结果无法确认，禁止重试。"
            ) from error

    def send_approved_image(self, path: Path) -> SendReceipt:
        """CS-3 is read-only; media sending begins in CS-6."""
        del path
        raise ReadOnlyAdapterError("CS-3 只读适配阶段禁止发送图片。")

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        """Change the selected order price once through the top-right chat control."""
        exact_price = format_money(parse_money(approved_price, field_name="确认改价"))
        page = self._require_ready_page()
        if not self._selected_conversation_key:
            raise ChatAdapterError("改价前没有唯一选中的会话。")
        selectors = self._contract.selectors
        required = (
            selectors.price_change_button_selector,
            selectors.price_change_dialog_selector,
            selectors.price_change_input_selector,
            selectors.price_change_confirm_selector,
            selectors.price_change_confirmation_dialog_selector,
            selectors.price_change_final_confirm_selector,
        )
        if not all(required):
            raise ReadOnlyAdapterError("客服页面改价契约尚未配置。")
        button = page.locator(selectors.price_change_button_selector or "")
        button_count = button.count()
        if button_count == 0:
            raise PriceChangeNotPerformedError("未找到右上角修改价格按钮，未改价。")
        if button_count > 1:
            raise PriceChangeNotPerformedError(
                f"右上角修改价格按钮匹配到 {button_count} 个节点，未改价。"
            )
        try:
            clicked = button.evaluate(
                """element => {
                    if (!element.isConnected) return false;
                    element.click();
                    return true;
                }"""
            )
            if clicked is not True:
                raise RuntimeError("price-change button detached")
            page.wait_for_selector(
                selectors.price_change_dialog_selector or "",
                state="visible",
                timeout=5_000,
            )
            dialog = page.locator(selectors.price_change_dialog_selector or "")
            price_inputs = dialog.locator(selectors.price_change_input_selector or "").all()
            confirm = dialog.locator(selectors.price_change_confirm_selector or "")
            if dialog.count() != 1 or len(price_inputs) not in {1, 2} or confirm.count() != 1:
                raise PriceChangeNotPerformedError("改价弹窗结构不唯一，未提交。")
            price_input = price_inputs[0]
            previous = price_input.input_value().strip() or price_input.get_attribute("placeholder")
            price_input.fill(exact_price, timeout=5_000)
            if format_money(parse_money(price_input.input_value(), field_name="页面金额")) != exact_price:
                raise PriceChangeNotPerformedError("改价输入框回读不一致，未提交。")
            if len(price_inputs) == 2:
                shipping_input = price_inputs[1]
                shipping_input.fill("0", timeout=5_000)
                shipping_value = shipping_input.input_value().strip()
                if (
                    format_money(parse_nonnegative_money(shipping_value, field_name="页面运费"))
                    != "0.00"
                ):
                    raise PriceChangeNotPerformedError("运费输入框未能归零，未提交。")
            if not confirm.is_enabled():
                raise PriceChangeNotPerformedError("确定修改按钮当前不可用，未提交。")
        except PriceChangeNotPerformedError:
            raise
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            self._record_send_error(page, "改价弹窗打开与金额复核", error)
            raise PriceChangeNotPerformedError(
                f"改价提交前页面校验失败（{type(error).__name__}），未改价。"
            ) from error

        confirmation_selector = selectors.price_change_confirmation_dialog_selector or ""
        final_confirm_selector = selectors.price_change_final_confirm_selector or ""
        try:
            # Xianyu's first button only opens a second confirmation dialog. It
            # is not the irreversible price action, but it is still clicked at
            # most once per operator-approved task.
            clicked = confirm.evaluate(
                """element => {
                    if (!element.isConnected || element.disabled) return false;
                    element.click();
                    return true;
                }"""
            )
            if clicked is not True:
                raise PriceChangeNotPerformedError("确定修改按钮已失效，未改价。")
            page.wait_for_selector(
                final_confirm_selector,
                state="visible",
                timeout=5_000,
            )
            confirmation_prompt = page.locator(confirmation_selector)
            final_confirm = page.locator(final_confirm_selector)
            if confirmation_prompt.count() < 1 or final_confirm.count() != 1:
                raise PriceChangeNotPerformedError("改价二次确认框结构不唯一，未改价。")
            if not final_confirm.is_enabled():
                raise PriceChangeNotPerformedError("改价二次确认按钮当前不可用，未改价。")
        except PriceChangeNotPerformedError:
            raise
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            self._record_send_error(page, "打开改价二次确认框", error)
            raise PriceChangeNotPerformedError(
                f"未能打开改价二次确认框（{type(error).__name__}），未改价。"
            ) from error

        receipt = PriceChangeReceipt(previous_price=previous, applied_price=exact_price)
        try:
            # This is the one irreversible price action. Never retry this click.
            clicked = final_confirm.evaluate(
                """element => {
                    if (!element.isConnected) return false;
                    const control = element.closest('button, [role="button"]') || element;
                    if (control.disabled || control.getAttribute('aria-disabled') === 'true') {
                        return false;
                    }
                    control.click();
                    return true;
                }"""
            )
            if clicked is not True:
                raise RuntimeError("final price confirm detached or disabled")
            page.wait_for_selector(
                final_confirm_selector,
                state="hidden",
                timeout=8_000,
            )
            page.wait_for_selector(
                selectors.price_change_dialog_selector or "",
                state="hidden",
                timeout=8_000,
            )
            return receipt
        except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError) as error:
            # Both dialogs disappearing proves the final click was accepted.
            # Otherwise the result is deliberately ambiguous and never retried.
            try:
                if (
                    page.locator(final_confirm_selector).count() == 0
                    and page.locator(selectors.price_change_dialog_selector or "").count() == 0
                ):
                    return receipt
            except (AttributeError, OSError, PlaywrightError, RuntimeError, TypeError, ValueError):
                pass
            self._record_send_error(page, "改价二次确认单次点击", error)
            raise ChatAdapterError(
                f"最终确定点击后结果无法确认（{type(error).__name__}），禁止自动重试。"
            ) from error

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        """Confirm a newly rendered outgoing message matches the approved text."""
        baseline = self._send_baselines.get(receipt.content_fingerprint)
        if baseline is None:
            return False
        page = self._require_ready_page()
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        for _ in range(10):
            snapshot = self.read_conversation()
            if any(
                message.direction is MessageDirection.OUTGOING
                and message.message_key not in baseline
                and content_fingerprint(message.text or "") == receipt.content_fingerprint
                for message in snapshot.messages
            ):
                self._send_baselines.pop(receipt.content_fingerprint, None)
                return True
            if callable(wait_for_timeout):
                wait_for_timeout(250)
        self._send_baselines.pop(receipt.content_fingerprint, None)
        return False

    def _record_send_error(self, page: PageLike, stage: str, error: Exception) -> None:
        detail = str(error).replace("\r", " ").replace("\n", " ")[:450]
        self._diagnostics.record_failure(
            page,
            f"文本发送阶段={stage}; exception={type(error).__name__}; detail={detail}",
        )

    def _require_page(self) -> PageLike:
        if self._page is None:
            raise ChatAdapterError("客服专用页尚未创建。")
        return self._page

    def _require_ready_page(self) -> PageLike:
        self._contract.validate()
        health = self.check_page_health()
        if not health.is_healthy:
            raise ChatAdapterError(health.detail or "客服页面不健康。")
        return self._require_page()

    def _read_product_card(self, page: PageLike) -> tuple[str | None, str | None]:
        selector = self._contract.selectors.product_card
        if not selector:
            return None, None
        card = page.locator(selector)
        if card.count() != 1:
            return None, None
        title = card.get_attribute(self._contract.selectors.product_title_attribute)
        if self._contract.selectors.product_title_selector:
            title_locator = card.locator(self._contract.selectors.product_title_selector)
            if title_locator.count() == 1:
                title = title_locator.inner_text().strip() or title
        product_id = card.get_attribute(self._contract.selectors.product_id_attribute)
        if not product_id and self._contract.selectors.product_id_selector:
            link = card.locator(self._contract.selectors.product_id_selector)
            if link.count() == 1:
                href = link.get_attribute("href")
                if href:
                    product_id = _query_parameter(href, "id")
        return product_id, title or card.inner_text().strip() or None

    def _current_conversation_key(self, page: PageLike) -> str:
        selectors = self._contract.selectors
        # open_conversation records the stable React session key before the
        # click. The rendered header often has no stable DOM attribute and
        # must not replace that key with a text hash.
        if self._selected_conversation_key:
            return self._selected_conversation_key
        if selectors.active_conversation_selector:
            active = page.locator(selectors.active_conversation_selector)
            if active.count() == 1:
                return self._conversation_key(active, 0)
        if selectors.current_conversation_selector:
            current = page.locator(selectors.current_conversation_selector)
            if current.count() == 1:
                return self._conversation_key(current, 0)
        items = page.locator(selectors.conversation_items).all()
        if len(items) == 1:
            return self._conversation_key(items[0], 0)
        raise ChatAdapterError("当前会话缺少稳定标识。")

    def _conversation_key(self, item: LocatorLike, index: int) -> str:
        selectors = self._contract.selectors
        key = item.get_attribute(selectors.conversation_key_attribute)
        if key:
            return key
        session_info = self._session_info(item)
        if session_info:
            session_id = _string(session_info.get("session_id"))
            if session_id:
                return f"session-{session_id}"
        normalized = " ".join(item.inner_text().split())
        digest = hashlib.sha256(f"conversation:{index}:{normalized}".encode()).hexdigest()
        return f"dom-{digest}"

    def _summary_from_item(
        self,
        item: LocatorLike,
        index: int,
        *,
        key: str | None = None,
        unread: bool | None = None,
        changed: str | None = None,
    ) -> ConversationSummary:
        selectors = self._contract.selectors
        info = self._session_info(item)
        if unread is None:
            unread = (item.get_attribute(selectors.unread_attribute) or "").casefold() in {
                "1", "true", "yes"
            }
            if selectors.unread_selector and item.locator(selectors.unread_selector).count() > 0:
                unread = True
        if changed is None:
            changed = item.get_attribute(selectors.changed_at_attribute)
        item_info = info.get("item_info", {}) if info else {}
        if not isinstance(item_info, dict):
            item_info = {}
        summary = info.get("summary", {}) if info else {}
        if not isinstance(summary, dict):
            summary = {}
        last_message = info.get("last_message", {}) if info else {}
        if not isinstance(last_message, dict):
            last_message = {}
        session_title = _string(info.get("title")) if info else None
        return ConversationSummary(
            conversation_key=key or self._conversation_key(item, index),
            display_name=session_title or item.inner_text().strip() or None,
            latest_message_key=None,
            has_unread=bool(unread),
            changed_at=_parse_page_timestamp(changed or (info or {}).get("operate_timestamp")),
            platform_product_id=_string(item_info.get("item_id"))
            or _string((info or {}).get("extension_item_id")),
            product_title=_string(item_info.get("title"))
            or _string((info or {}).get("extension_item_title")),
            listed_price=_string(item_info.get("price")),
            last_message_text=_string(last_message.get("message"))
            or _string(summary.get("summary_content")),
        )

    def _summary_from_row(self, row: dict[str, object], key: str) -> ConversationSummary:
        item_info = row.get("item_info")
        if not isinstance(item_info, dict):
            item_info = {}
        summary = row.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        last_message = row.get("last_message")
        if not isinstance(last_message, dict):
            last_message = {}
        display_name = _string(row.get("title")) or _string(row.get("text"))
        return ConversationSummary(
            conversation_key=key,
            display_name=display_name,
            has_unread=bool(row.get("unread")),
            changed_at=_parse_page_timestamp(row.get("changed_at") or row.get("operate_timestamp")),
            platform_product_id=_string(item_info.get("item_id")) or _string(row.get("extension_item_id")),
            product_title=_string(item_info.get("title")) or _string(row.get("extension_item_title")),
            listed_price=_string(item_info.get("price")),
            last_message_text=_string(last_message.get("message"))
            or _string(summary.get("summary_content")),
        )

    def _visible_session_rows(self, page: PageLike) -> list[dict[str, object]]:
        """Extract virtualized rows in one DOM call so React re-renders cannot move locators."""
        locator = page.locator(self._contract.selectors.conversation_items)
        evaluate_all = getattr(locator, "evaluate_all", None)
        if not callable(evaluate_all):
            rows: list[dict[str, object]] = []
            for index, item in enumerate(locator.all()):
                info = self._session_info(item) or {}
                rows.append(
                    {
                        "key": self._conversation_key(item, index),
                        "title": info.get("title"),
                        "text": item.inner_text(),
                        "unread": False,
                        "item_info": info.get("item_info", {}),
                        "summary": info.get("summary", {}),
                        "last_message": info.get("last_message", {}),
                    }
                )
            return rows
        try:
            value = evaluate_all(
                """elements => elements.map(element => {
                    const fiberKey = Object.keys(element).find(key => key.startsWith('__reactFiber'));
                    let fiber = fiberKey ? element[fiberKey] : null;
                    while (fiber && !(fiber.memoizedProps && fiber.memoizedProps.sessionInfo)) {
                        fiber = fiber.return;
                    }
                    const session = fiber?.memoizedProps?.sessionInfo;
                    const itemInfo = session?.itemInfo || {};
                    const extension = session?.extension || {};
                    const summary = session?.summary || {};
                    const lastMessage = session?.lastMessage || {};
                    return {
                        key: element.getAttribute('data-conversation-key') ||
                            (session?.sessionId ? `session-${session.sessionId}` : null),
                        title: session?.title ?? null,
                        text: element.innerText || '',
                        unread: Boolean(session?.unread) || Boolean(element.querySelector('.ant-badge-count')),
                        changed_at: element.getAttribute('data-changed-at'),
                        operate_timestamp: session?.operateTimeStamp ?? null,
                        item_info: {
                            item_id: itemInfo.itemId ?? null,
                            title: itemInfo.title ?? null,
                            price: itemInfo.price ?? null,
                        },
                        extension_item_id: extension.itemId ?? null,
                        extension_item_title: extension.itemTitle ?? null,
                        summary: {summary_content: summary.summaryContent ?? null},
                        last_message: {message: lastMessage.message ?? null}
                    };
                })"""
            )
        except (PlaywrightError, RuntimeError, TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    def _session_info(self, item: LocatorLike) -> dict[str, object] | None:
        if not hasattr(item, "evaluate"):
            return None
        try:
            value = item.evaluate(
                """element => {
                    const fiberKey = Object.keys(element).find(key => key.startsWith('__reactFiber'));
                    let fiber = fiberKey ? element[fiberKey] : null;
                    while (fiber && !(fiber.memoizedProps && fiber.memoizedProps.sessionInfo)) {
                        fiber = fiber.return;
                    }
                    const session = fiber?.memoizedProps?.sessionInfo;
                    if (!session) return null;
                    const itemInfo = session.itemInfo || {};
                    const extension = session.extension || {};
                    const summary = session.summary || {};
                    const lastMessage = session.lastMessage || {};
                    return {
                        session_id: session.sessionId ?? null,
                        title: session.title ?? null,
                        item_info: {
                            item_id: itemInfo.itemId ?? null,
                            title: itemInfo.title ?? null,
                            price: itemInfo.price ?? null,
                        },
                        extension_item_id: extension.itemId ?? null,
                        extension_item_title: extension.itemTitle ?? null,
                        summary: {
                            summary_content: summary.summaryContent ?? null,
                            time_stamp: summary.timeStamp ?? null,
                        },
                        operate_timestamp: session.operateTimeStamp ?? null,
                        last_message: {message: lastMessage.message ?? null},
                        unread: Boolean(session.unread)
                    };
                }"""
            )
        except (AttributeError, PlaywrightError, RuntimeError, TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _session_info_from_page(self, page: PageLike) -> dict[str, object] | None:
        selectors = self._contract.selectors
        for index, item in enumerate(page.locator(selectors.conversation_items).all()):
            if self._conversation_key(item, index) == self._selected_conversation_key:
                return self._session_info(item)
        return None

    def _find_message(self, page: PageLike, message_key: str) -> LocatorLike:
        selectors = self._contract.selectors
        for index, item in enumerate(page.locator(selectors.message_items).all()):
            direction = _direction(item, selectors)
            kind = _kind(item, selectors)
            text = _item_text(item, selectors.text_selector)
            if self._message_key(item, index, direction, kind, text) == message_key:
                return item
        raise ChatAdapterError("未找到指定消息。")

    def _message_key(
        self,
        item: LocatorLike,
        index: int,
        direction: MessageDirection,
        kind: MessageKind,
        text: str,
    ) -> str:
        return item.get_attribute(self._contract.selectors.message_key_attribute) or hashlib.sha256(
            f"{index}:{direction.value}:{kind.value}:{text}".encode()
        ).hexdigest()


def _item_text(item: LocatorLike, selector: str | None) -> str:
    if selector:
        nested = item.locator(selector)
        if nested.count() != 0:
            return nested.inner_text().strip()
    return item.inner_text().strip()


def _direction(item: LocatorLike, selectors: ChatPageSelectors) -> MessageDirection:
    value = item.get_attribute(selectors.direction_attribute)
    if value is None:
        # The live page places the avatar before the content for incoming
        # rows and after it for outgoing rows.  Geometry is more reliable
        # than :scope selectors here because the page uses nested wrappers.
        if hasattr(item, "evaluate"):
            try:
                placement = item.evaluate(
                    """element => {
                        const content = element.querySelector('.message-content--kBUbolyy');
                        const avatar = element.querySelector('img.avatar--e05bt3Ju');
                        if (!content || !avatar) return null;
                        const contentRect = content.getBoundingClientRect();
                        const avatarRect = avatar.getBoundingClientRect();
                        return {contentX: contentRect.x, avatarX: avatarRect.x};
                    }"""
                )
                if isinstance(placement, dict):
                    content_x = float(placement["contentX"])
                    avatar_x = float(placement["avatarX"])
                    if avatar_x < content_x:
                        return MessageDirection.INCOMING
                    if avatar_x > content_x:
                        return MessageDirection.OUTGOING
            except (KeyError, TypeError, ValueError):
                pass
        if selectors.incoming_direction_selector and item.locator(selectors.incoming_direction_selector).count() > 0:
            return MessageDirection.INCOMING
        if selectors.outgoing_direction_selector and item.locator(selectors.outgoing_direction_selector).count() > 0:
            return MessageDirection.OUTGOING
    try:
        return MessageDirection((value or "").casefold())
    except ValueError as error:
        raise ChatAdapterError("消息方向不在已知枚举中。") from error


def _kind(item: LocatorLike, selectors: ChatPageSelectors) -> MessageKind:
    value = item.get_attribute(selectors.kind_attribute)
    if value is None:
        if selectors.image_kind_selector and item.locator(selectors.image_kind_selector).count() > 0:
            return MessageKind.IMAGE
        if selectors.voice_kind_selector and item.locator(selectors.voice_kind_selector).count() > 0:
            return MessageKind.VOICE
    try:
        return MessageKind((value or "text").casefold())
    except ValueError:
        return MessageKind.UNKNOWN


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ChatAdapterError("页面时间格式无效。") from error


def _parse_page_timestamp(value: object) -> datetime | None:
    """Normalize DOM ISO timestamps and the millisecond values used by React state."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1_000
        try:
            return datetime.fromtimestamp(number).astimezone()
        except (OverflowError, OSError, ValueError) as error:
            raise ChatAdapterError("页面时间格式无效。") from error
    return _parse_datetime(str(value))


def _string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fallback_conversation_key(row: dict[str, object], index: int) -> str:
    normalized = " ".join(str(row.get("text") or "").split())
    digest = hashlib.sha256(f"conversation:{index}:{normalized}".encode()).hexdigest()
    return f"dom-{digest}"


def _query_parameter(url: str, name: str) -> str | None:
    """Read one non-sensitive identifier from a verified product link."""
    values = parse_qs(urlparse(url).query).get(name, [])
    return values[0] if values and values[0] else None


def _clamp_clip(box: dict[str, object], viewport: dict[str, object]) -> dict[str, float] | None:
    """Clamp a DOM rectangle to the current viewport for page screenshots."""
    try:
        width = float(viewport["width"])
        height = float(viewport["height"])
        left = max(0.0, min(float(box["x"]), width))
        top = max(0.0, min(float(box["y"]), height))
        right = max(left, min(float(box["x"]) + float(box["width"]), width))
        bottom = max(top, min(float(box["y"]) + float(box["height"]), height))
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}

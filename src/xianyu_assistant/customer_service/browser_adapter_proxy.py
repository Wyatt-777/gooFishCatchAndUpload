"""Synchronous business adapter backed by a killable browser-only process."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from xianyu_assistant.browser.executor_protocol import (
    BrowserExecutorError,
    BrowserOperationCancelled,
    BrowserOperationTimeout,
)
from xianyu_assistant.browser.executor_supervisor import BrowserExecutorSupervisor
from xianyu_assistant.customer_service.browser_adapter import (
    ChatAdapterError,
    ChatPageContract,
    ReadOnlyAdapterError,
)
from xianyu_assistant.customer_service.models import (
    ConversationSnapshot,
    ConversationSummary,
    ImagePayload,
    PageHealth,
    PageHealthStatus,
    PriceChangeReceipt,
    SendReceipt,
)
from xianyu_assistant.customer_service.protocols import (
    PriceChangeNotPerformedError,
    TextSendNotPerformedError,
)


class BrowserAdapterProxy:
    """Keep checkpoint callbacks and durable evidence exclusively in the parent."""

    def __init__(self, supervisor: BrowserExecutorSupervisor, contract: ChatPageContract) -> None:
        self._supervisor = supervisor
        self._contract = contract
        self._send_checkpoint: Callable[[dict[str, Any]], Any] | None = None
        self._price_checkpoint: Callable[[dict[str, Any]], Any] | None = None
        self._selected_conversation_key: str | None = None
        self._infrastructure_failure: BrowserExecutorError | None = None
        self._supervisor.set_contract(contract)

    @property
    def epoch(self) -> int:
        return self._supervisor.epoch

    @property
    def infrastructure_failure(self) -> BrowserExecutorError | None:
        return self._infrastructure_failure

    def progress_snapshot(self) -> dict[str, Any]:
        return self._supervisor.progress_snapshot()

    def reset_after_disconnect(self) -> None:
        self._selected_conversation_key = None
        self._infrastructure_failure = None

    def _checkpoint(self, action: str, evidence: dict[str, Any]) -> Any:
        callback = self._send_checkpoint if action == "text" else self._price_checkpoint
        if callback is None:
            raise ChatAdapterError("父进程尚未批准本次提交，未发送或改价。")
        return callback(evidence)

    def _call(self, operation: str, *args: Any) -> Any:
        try:
            return self._supervisor.call(operation, *args, checkpoint=self._checkpoint)
        except BrowserExecutorError as error:
            self._infrastructure_failure = error
            raise

    def open_dedicated_chat_page(self) -> None:
        self._call("open_dedicated_chat_page")

    def check_page_health(self) -> PageHealth:
        if not self._supervisor.is_connected:
            return PageHealth(PageHealthStatus.UNAVAILABLE, "浏览器执行者不可用，需要恢复。")
        return self._call("check_page_health")

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        return self._call("list_changed_conversations", limit)

    def list_all_conversations(self) -> list[ConversationSummary]:
        return self._call("list_all_conversations")

    def open_conversation(self, conversation_key: str) -> None:
        self._call("open_conversation", conversation_key)
        self._selected_conversation_key = conversation_key

    def read_conversation(self) -> ConversationSnapshot:
        return self._call("read_conversation")

    def request_voice_transcript(self, message_key: str) -> str | None:
        return self._call("request_voice_transcript", message_key)

    def capture_incoming_image(self, message_key: str) -> ImagePayload:
        return self._call("capture_incoming_image", message_key)

    def set_send_checkpoint(self, callback: Callable[[dict[str, Any]], Any] | None) -> None:
        self._send_checkpoint = callback
        if callback is not None:
            self._supervisor.reset_evidence("text")

    def send_evidence(self) -> dict[str, Any]:
        return self._supervisor.evidence("text")

    def send_text(self, text: str) -> SendReceipt:
        try:
            return self._call("send_text", text)
        except (BrowserOperationTimeout, BrowserOperationCancelled) as error:
            evidence = self.send_evidence()
            if (not error.diagnostic.get("submission_authorized")
                    and error.diagnostic.get("executor_exit_confirmed")):
                evidence.update(stage="preflight", click_state="not_started")
                # A typed pre-click result is justified only after the old actor has exited.
                raise TextSendNotPerformedError(
                    "浏览器执行者在提交许可前停止，未执行提交。", retryable=False,
                ) from error
            raise

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        return self._call("verify_outgoing", receipt)

    def verified_outgoing_message_key(self, receipt: SendReceipt) -> str | None:
        return self._call("verified_outgoing_message_key", receipt)

    def reconcile_send(self, evidence: dict[str, Any], fingerprint: str) -> tuple[str, str | None]:
        return self._call("reconcile_send", evidence, fingerprint)

    def send_approved_image(self, path: Path) -> SendReceipt:
        del path
        raise ReadOnlyAdapterError("当前版本禁止发送图片。")

    def set_price_checkpoint(self, callback: Callable[[dict[str, Any]], Any] | None) -> None:
        self._price_checkpoint = callback
        if callback is not None:
            self._supervisor.reset_evidence("price")

    def price_evidence(self) -> dict[str, Any]:
        return self._supervisor.evidence("price")

    def verify_order_price(self, evidence: dict[str, Any]) -> bool:
        return self._call("verify_order_price", evidence)

    def can_verify_order_price(self) -> bool:
        selectors = self._contract.selectors
        return bool((selectors.order_price_selector and selectors.order_shipping_price_selector)
                    or selectors.price_dialog_readback_enabled)

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        try:
            return self._call("change_order_price", approved_price)
        except (BrowserOperationTimeout, BrowserOperationCancelled) as error:
            if (not error.diagnostic.get("submission_authorized")
                    and error.diagnostic.get("executor_exit_confirmed")):
                raise PriceChangeNotPerformedError(
                    "改价最终提交许可尚未发出，已停止执行者，未修改订单。",
                ) from error
            raise

"""Hidden browser-only entry point; it never creates Qt, LLM or database objects."""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from multiprocessing.connection import Client, Connection
from pathlib import Path
from typing import Any

from xianyu_assistant.browser.executor_protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    BrowserExecutorError,
    BrowserOperationCancelled,
    decode_error,
    encode_error,
    pack_message,
    unpack_message,
)
from xianyu_assistant.browser.manager import BrowserConnectionConfig, BrowserManager
from xianyu_assistant.customer_service.browser_adapter import (
    ChatPageContract,
    PlaywrightXianyuChatAdapter,
)
from xianyu_assistant.customer_service.diagnostics import ChatAdapterDiagnostics
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
    PageHealth,
    PageHealthStatus,
)

_ADAPTER_OPERATIONS = frozenset({
    "open_dedicated_chat_page", "check_page_health", "list_changed_conversations",
    "list_all_conversations", "open_conversation", "read_conversation",
    "request_voice_transcript", "capture_incoming_image", "send_text", "verify_outgoing",
    "verified_outgoing_message_key", "reconcile_send", "change_order_price",
    "verify_order_price",
})
_STACK_LOG_LIMIT_BYTES = 5 * 1024 * 1024


def _prune_stack_logs(root: Path) -> None:
    """Retain only this executor's named stack files for 15 days and at most 50 files."""
    resolved_root = root.resolve()
    candidates = []
    for path in root.glob("executor-*-stack.log"):
        if not re.fullmatch(r"executor-[0-9]+-stack\.log", path.name):
            continue
        try:
            if path.is_symlink() or path.resolve().parent != resolved_root or not path.is_file():
                continue
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    cutoff = time.time() - 15 * 24 * 60 * 60
    for index, (modified, path) in enumerate(sorted(candidates, key=lambda item: item[0], reverse=True)):
        # Reserve one slot for the log opened immediately after this cleanup.
        if modified >= cutoff and index < 49:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue  # A currently opened diagnostic file can be retained until the next startup.


class BrowserExecutorChild:
    def __init__(
        self, connection: Connection, config: BrowserConnectionConfig, epoch: int,
        *, self_test: bool = False, contract: ChatPageContract | None = None,
    ) -> None:
        self.connection = connection
        self.epoch = epoch
        self.self_test = self_test
        self.manager = BrowserManager(config)
        self.manager.set_owned_page_callback(self._target_created)
        self.adapter: PlaywrightXianyuChatAdapter | None = None
        self.command: dict[str, Any] = {}
        self.deadline = 0.0
        if contract is not None:
            self._configure_contract(contract)

    def _event(self, kind: str, **payload: Any) -> None:
        self.connection.send_bytes(pack_message({
            "kind": kind, "epoch": self.epoch,
            "request_id": self.command["request_id"], **payload,
        }))

    def _target_created(self, target_id: str) -> None:
        self._event("target_created", target_id=target_id)

    def _configure_contract(self, contract: ChatPageContract) -> None:
        contract.validate()
        if self.adapter is not None:
            raise BrowserExecutorError("客服页面契约不能在已初始化的执行者内切换。")
        self.adapter = PlaywrightXianyuChatAdapter(
            self.manager, contract, diagnostics=ChatAdapterDiagnostics(metadata_only=True),
        )
        self.adapter.set_send_checkpoint(lambda evidence: self._checkpoint("text", evidence))
        self.adapter.set_price_checkpoint(lambda evidence: self._checkpoint("price", evidence))

    def _checkpoint(self, action: str, evidence: dict[str, Any]) -> Any:
        """No page submission happens until the parent's durable writes allow it."""
        self._event("checkpoint", action=action, evidence=evidence)
        while True:
            if time.monotonic() >= self.deadline:
                raise BrowserOperationCancelled("提交许可等待超时，未继续点击。")
            if not self.connection.poll(min(0.1, max(0.001, self.deadline - time.monotonic()))):
                continue
            response = unpack_message(self.connection.recv_bytes(MAX_MESSAGE_BYTES))
            if (response.get("epoch") != self.epoch
                    or response.get("request_id") != self.command["request_id"]):
                raise BrowserExecutorError("提交许可来自过期浏览器请求。")
            if response.get("kind") == "denied":
                raise decode_error(response["error"])
            if response.get("kind") != "permit":
                raise BrowserOperationCancelled("未取得父进程提交许可，未继续点击。")
            if time.monotonic() >= self.deadline:
                raise BrowserOperationCancelled("提交许可已过期，未继续点击。")
            return response.get("permit_id")

    def _send_evidence(self, operation: str) -> None:
        if self.adapter is None:
            return
        if operation in {"send_text", "verify_outgoing"}:
            self._event("evidence", action="text", evidence=self.adapter.send_evidence())
        elif operation in {"change_order_price", "verify_order_price"}:
            self._event("evidence", action="price", evidence=self.adapter.price_evidence())

    def run(self) -> int:
        trace_stream = None
        if not self.self_test:
            try:
                trace_root = (Path(os.environ.get("LOCALAPPDATA", str(Path.cwd())))
                              / "XianyuAssistant" / "diagnostics" / "customer_service")
                trace_root.mkdir(parents=True, exist_ok=True)
                _prune_stack_logs(trace_root)
                trace_stream = (trace_root / f"executor-{os.getpid()}-stack.log").open(
                    "a", encoding="utf-8",
                )
            except OSError:
                trace_stream = None
        try:
            return self._run_commands(trace_stream)
        finally:
            faulthandler.cancel_dump_traceback_later()
            if trace_stream is not None:
                trace_stream.close()

    def _run_commands(self, trace_stream: Any) -> int:
        while True:
            try:
                command = unpack_message(self.connection.recv_bytes(MAX_MESSAGE_BYTES))
            except (EOFError, OSError):
                return 0
            if (command.get("kind") != "command" or command.get("epoch") != self.epoch
                    or not isinstance(command.get("request_id"), str)):
                return 2
            self.command = command
            operation = str(command.get("operation"))
            budget = float(command.get("deadline_seconds", 0))
            if not 0 < budget <= 150:
                return 2
            self.deadline = time.monotonic() + budget
            if trace_stream is not None:
                faulthandler.cancel_dump_traceback_later()
                if os.fstat(trace_stream.fileno()).st_size > _STACK_LOG_LIMIT_BYTES:
                    # Keep the same descriptor so subsequent faulthandler writes
                    # remain valid; cancel the previous dump before truncating.
                    trace_stream.seek(0)
                    trace_stream.truncate(0)
                trace_stream.write(f"operation={operation}; request={command['request_id']}; epoch={self.epoch}\n")
                trace_stream.flush()
                faulthandler.dump_traceback_later(max(0.1, budget - 0.5), file=trace_stream)
            args = command.get("args", ())
            try:
                if operation == "self_test":
                    result = {"protocol": PROTOCOL_VERSION, "epoch": self.epoch,
                              "pid": os.getpid(), "cdp_connected": False,
                              "codec": _codec_self_check()}
                elif self.self_test and operation != "close":
                    raise BrowserExecutorError("自检模式禁止连接或操作浏览器。")
                elif operation == "connect":
                    self.manager.connect()
                    result = True
                elif operation == "configure_contract":
                    self._configure_contract(*args)
                    result = None
                elif operation == "close_owned_targets":
                    self.manager.close_owned_targets(*args)
                    result = None
                elif operation == "close":
                    self.manager.disconnect()
                    result = None
                elif operation in _ADAPTER_OPERATIONS and self.adapter is not None:
                    result = getattr(self.adapter, operation)(*args)
                else:
                    raise BrowserExecutorError("浏览器执行请求未在允许的操作列表内。")
                self._send_evidence(operation)
                self._event("result", result=result)
                if operation == "close":
                    return 0
            except Exception as error:  # noqa: BLE001 - child marshals every operation failure
                self._send_evidence(operation)
                self._event("error", error=encode_error(error))
            finally:
                faulthandler.cancel_dump_traceback_later()


def _codec_self_check() -> bool:
    value = PageHealth(PageHealthStatus.HEALTHY)
    snapshot = ConversationSnapshot("codec-check", (ChatMessage(
        "message-check", MessageDirection.INCOMING, MessageKind.TEXT, text="codec",
        platform_time=datetime(2026, 1, 1, tzinfo=UTC),
    ),))
    expected = {"value": value, "bytes": b"local-IPC", "snapshot": snapshot}
    decoded = unpack_message(pack_message(expected))
    return decoded == expected


def worker_main(argv: Sequence[str] | None = None) -> int:
    """Dispatch before QApplication/single-instance locks in a frozen executable."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--browser-worker", action="store_true")
    parser.add_argument("--executor-address")
    parser.add_argument("--executor-self-test", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    arguments, _unknown = parser.parse_known_args(argv)
    if arguments.self_check and not arguments.executor_address:
        report = {"protocol": PROTOCOL_VERSION, "codec": _codec_self_check(),
                  "cdp_connected": False}
        report_path = os.environ.get("XIANYU_BROWSER_EXECUTOR_REPORT")
        if report_path:
            Path(report_path).write_text(json.dumps(report), encoding="utf-8")
        return 0 if report["codec"] else 2
    if not arguments.executor_address or os.name != "nt":
        return 2
    auth = os.environ.pop("XIANYU_BROWSER_EXECUTOR_AUTH", "")
    epoch_value = os.environ.pop("XIANYU_BROWSER_EXECUTOR_EPOCH", "")
    if len(auth) != 64 or not epoch_value.isdigit():
        return 2
    connection: Connection | None = None
    try:
        connection = Client(arguments.executor_address, family="AF_PIPE", authkey=bytes.fromhex(auth))
        connection.send_bytes(pack_message({"kind": "bootstrap", "epoch": int(epoch_value),
                                            "pid": os.getpid()}))
        initialize = unpack_message(connection.recv_bytes(MAX_MESSAGE_BYTES))
        epoch = int(epoch_value)
        if (initialize.get("kind") != "initialize"
                or initialize.get("version") != PROTOCOL_VERSION
                or initialize.get("epoch") != epoch
                or not isinstance(initialize.get("config"), BrowserConnectionConfig)):
            return 2
        actor = BrowserExecutorChild(
            connection, initialize["config"], epoch,
            self_test=arguments.executor_self_test, contract=initialize.get("contract"),
        )
        connection.send_bytes(pack_message({"kind": "ready", "epoch": epoch, "pid": os.getpid()}))
        return actor.run()
    except (OSError, EOFError, ValueError, TypeError):
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))

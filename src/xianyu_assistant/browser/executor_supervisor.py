"""Parent-owned deadlines and lifecycle for the isolated browser actor."""

from __future__ import annotations

import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any
from uuid import uuid4

from xianyu_assistant.browser.executor_fence import load_retired_fence, write_fence
from xianyu_assistant.browser.executor_protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    BrowserExecutorError,
    BrowserOperationCancelled,
    BrowserOperationTimeout,
    decode_error,
    encode_error,
    pack_message,
    unpack_message,
)
from xianyu_assistant.browser.manager import BrowserConnectionConfig
from xianyu_assistant.browser.windows_job import WindowsJob, process_identity
from xianyu_assistant.customer_service.browser_adapter import ChatAdapterError, ChatPageContract
from xianyu_assistant.customer_service.protocols import (
    PriceChangeNotPerformedError,
    TextSendNotPerformedError,
)

logger = logging.getLogger(__name__)

OPERATION_TIMEOUTS = {
    "connect": 20.0, "open_dedicated_chat_page": 20.0, "check_page_health": 15.0,
    "list_changed_conversations": 6.0, "open_conversation": 8.0,
    "read_conversation": 7.0, "request_voice_transcript": 8.0,
    "capture_incoming_image": 8.0, "send_text": 25.0, "verify_outgoing": 8.0,
    "reconcile_send": 8.0, "change_order_price": 30.0, "verify_order_price": 30.0,
    "close_owned_targets": 8.0, "close": 2.0, "self_test": 3.0,
    "configure_contract": 3.0, "list_all_conversations": 30.0,
    "verified_outgoing_message_key": 2.0,
}


class BrowserExecutorSupervisor:
    """One local actor, one in-flight command, and no ownership of user Chrome."""

    def __init__(
        self,
        config: BrowserConnectionConfig,
        *,
        stop_event: threading.Event | None = None,
        operation_recorder: Callable[[dict[str, Any]], None] | None = None,
        fence_path: Path | None = None,
    ) -> None:
        self._config = config
        self._stop_event = stop_event or threading.Event()
        self._recorder = operation_recorder
        self._contract: ChatPageContract | None = None
        self._process: subprocess.Popen | None = None
        self._job: WindowsJob | None = None
        self._connection: Connection | None = None
        self._listener: Listener | None = None
        self._call_lock = threading.RLock()
        self._resource_lock = threading.RLock()
        self._epoch = 0
        self._session_id = uuid4().hex
        self._fence_path = fence_path or (
            Path(os.environ.get("LOCALAPPDATA", str(Path.cwd())))
            / "XianyuAssistant" / "browser-executor-fence.json"
        )
        self._known_processes: dict[int, dict[str, int]] = {}
        self._self_test_mode = False
        self._owns_fence = False
        self._connected = False
        self._active: dict[str, Any] | None = None
        self._last: dict[str, Any] = {"state": "stopped", "epoch": 0}
        self._owned_targets: set[str] = set()
        self._conversation_key: str | None = None
        self._evidence: dict[str, dict[str, Any]] = {"text": {}, "price": {}}
        self._cleanup_error: str | None = None
        self._timed_out: set[str] = set()
        self._cancelled: set[str] = set()

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def timeout_ms(self) -> int:
        return self._config.timeout_ms

    @property
    def is_connected(self) -> bool:
        process = self._process
        return bool(self._connected and process is not None and process.poll() is None)

    @property
    def owned_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._owned_targets))

    def set_operation_recorder(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._recorder = callback

    def progress_snapshot(self) -> dict[str, Any]:
        # Publishing/replacing dictionaries uses the GIL. Never wait for a cleanup
        # lock here: the UI must keep updating while an actor is being terminated.
        result = dict(self._active or self._last)
        started = result.pop("started_monotonic", None)
        if started is not None:
            result["elapsed_seconds"] = max(0.0, time.monotonic() - started)
        return result

    def evidence(self, action: str) -> dict[str, Any]:
        return dict(self._evidence[action])

    def reset_evidence(self, action: str) -> None:
        self._evidence[action] = {"stage": "preflight", "click_state": "not_started"}

    def set_contract(self, contract: ChatPageContract) -> None:
        self._contract = contract
        if self._connection is not None:
            self.call("configure_contract", contract)

    def _record(self, event: str, data: dict[str, Any]) -> None:
        if self._recorder is not None:
            self._recorder({**data, "event": event})

    def _persist_fence(self, state: str) -> None:
        if self._self_test_mode or self._job is None:
            return
        for pid in self._job.process_ids():
            identity = process_identity(pid)
            if identity is not None:
                self._known_processes[pid] = identity
        write_fence(self._fence_path, {
            "version": 1, "state": state, "epoch": self._epoch,
            "actor_id": f"browser-{self._session_id}-{self._epoch}",
            "job_name": self._job.name, "processes": list(self._known_processes.values()),
            "owned_targets": sorted(self._owned_targets),
        })
        self._owns_fence = True

    def _begin(self, operation: str, timeout: float) -> tuple[dict[str, Any], threading.Timer]:
        record = {
            "operation": operation, "request_id": uuid4().hex, "epoch": self._epoch,
            "actor_id": f"browser-{self._session_id}-{self._epoch}",
            "pid": self._process.pid if self._process else None,
            "deadline_seconds": timeout, "started_monotonic": time.monotonic(),
            "state": "running", "submission_authorized": False,
            "conversation_key": self._conversation_key,
        }
        with self._resource_lock:
            self._active = record
        timer = threading.Timer(timeout, self._expire, args=(record["request_id"],))
        timer.daemon = True
        timer.start()
        try:
            self._record("start", self.progress_snapshot())
        except Exception:
            timer.cancel()
            self._force_close()
            self._active = None
            raise
        return record, timer

    def _expire(self, request_id: str) -> None:
        with self._resource_lock:
            if self._active is None or self._active["request_id"] != request_id:
                return
            self._timed_out.add(request_id)
            self._active["state"] = "terminating"
        self._force_close()

    def _check(self, record: dict[str, Any], *, allow_stop: bool = False) -> None:
        request_id = record["request_id"]
        if request_id in self._timed_out or (
            time.monotonic() - record["started_monotonic"] >= record["deadline_seconds"]
        ):
            self._timed_out.add(request_id)
            self._force_close()
            raise BrowserOperationTimeout(
                f"浏览器操作 {record['operation']} 超过 {record['deadline_seconds']:g} 秒，已隔离执行者。",
                self._diagnostic(record),
            )
        if request_id in self._cancelled or (self._stop_event.is_set() and not allow_stop):
            self._force_close()
            raise BrowserOperationCancelled("客服浏览器操作已停止。", self._diagnostic(record))

    def _diagnostic(self, record: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in record.items() if key != "started_monotonic"}
        data["elapsed_seconds"] = max(0.0, time.monotonic() - record["started_monotonic"])
        data["executor_exit_confirmed"] = self._process is None and self._cleanup_error is None
        if self._cleanup_error:
            data["cleanup_error"] = self._cleanup_error
        return data

    def _finish(self, record: dict[str, Any], timer: threading.Timer, outcome: str) -> None:
        timer.cancel()
        with self._resource_lock:
            if self._active is record:
                self._active = None
            self._last = {**self._diagnostic(record), "state": outcome}
        try:
            self._record(outcome, dict(self._last))
        except Exception:
            self._force_close()
            raise
        self._timed_out.discard(record["request_id"])
        self._cancelled.discard(record["request_id"])

    def start(self, *, self_test: bool = False, timeout_seconds: float = 20.0) -> None:
        """Bootstrap/authenticate a hidden child; no CDP is used in self-test mode."""
        with self._call_lock:
            if (self._process is not None and self._process.poll() is None
                    and self._connection is not None):
                return
            if self._process is not None or self._job is not None or self._connection is not None:
                self._force_close()
            if self._cleanup_error:
                raise BrowserExecutorError("旧浏览器执行者退出未确认，禁止创建新执行者。")
            self._self_test_mode = self_test
            if not self_test:
                retired = load_retired_fence(self._fence_path)
                if retired:
                    self._owned_targets.update(str(target) for target in retired.get("owned_targets", []))
            self._epoch += 1
            record, timer = self._begin("start_executor", timeout_seconds)
            outcome = "error"
            try:
                if os.name != "nt":
                    raise BrowserExecutorError("浏览器执行进程目前仅支持 Windows 本机命名管道。")
                address = rf"\\.\pipe\XianyuAssistantBrowser-{uuid4().hex}"
                authkey = secrets.token_bytes(32)
                self._listener = Listener(address, family="AF_PIPE", authkey=authkey)
                accepted: queue.Queue = queue.Queue(maxsize=1)
                listener = self._listener

                def accept_connection() -> None:
                    try:
                        connection = listener.accept()
                        accepted.put(connection)
                    except Exception as error:  # noqa: BLE001 - transport thread reports to parent
                        accepted.put(error)

                accept_thread = threading.Thread(target=accept_connection, daemon=True)
                accept_thread.start()
                environment = os.environ.copy()
                environment["XIANYU_BROWSER_EXECUTOR_AUTH"] = authkey.hex()
                environment["XIANYU_BROWSER_EXECUTOR_EPOCH"] = str(self._epoch)
                command = [sys.executable]
                if not getattr(sys, "frozen", False):
                    command += ["-m", "xianyu_assistant.browser.executor_child"]
                    source_root = str(Path(__file__).resolve().parents[2])
                    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
                        source_root, environment.get("PYTHONPATH"),
                    )))
                command += ["--browser-worker", "--executor-address", address]
                if self_test:
                    command.append("--executor-self-test")
                job = WindowsJob(f"Local\\XianyuAssistantBrowser-{uuid4().hex}")
                self._job = job
                self._process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
                )
                job.assign(self._process)
                identity = process_identity(self._process.pid)
                if identity is not None:
                    self._known_processes[self._process.pid] = identity
                record["pid"] = self._process.pid
                while True:
                    self._check(record)
                    try:
                        connection = accepted.get(timeout=0.1)
                        break
                    except queue.Empty:
                        if self._process is None or self._process.poll() is not None:
                            raise BrowserExecutorError("浏览器执行进程在建立通道前退出。") from None
                if isinstance(connection, Exception):
                    raise BrowserExecutorError("本机浏览器执行通道无法建立。") from connection
                self._connection = connection
                self._listener.close()
                self._listener = None
                while not connection.poll(0.1):
                    self._check(record)
                bootstrap = unpack_message(connection.recv_bytes(MAX_MESSAGE_BYTES))
                if (bootstrap.get("kind") != "bootstrap" or bootstrap.get("epoch") != self._epoch
                        or not isinstance(bootstrap.get("pid"), int)):
                    raise BrowserExecutorError("浏览器执行进程身份握手失败。")
                job.assign_pid(bootstrap["pid"])
                identity = process_identity(bootstrap["pid"])
                if identity is None:
                    raise BrowserExecutorError("浏览器执行进程已退出。")
                self._known_processes[bootstrap["pid"]] = identity
                self._persist_fence("starting")
                self._check(record)
                connection.send_bytes(pack_message({
                    "kind": "initialize", "version": PROTOCOL_VERSION, "epoch": self._epoch,
                    "config": self._config, "contract": self._contract,
                }))
                while not connection.poll(0.1):
                    self._check(record)
                hello = unpack_message(connection.recv_bytes(MAX_MESSAGE_BYTES))
                self._check(record)
                if hello.get("kind") != "ready" or hello.get("epoch") != self._epoch:
                    raise BrowserExecutorError("浏览器执行通道握手不一致。")
                record["executor_pid"] = hello.get("pid")
                outcome = "complete"
            except (OSError, EOFError, ValueError, TypeError) as error:
                self._check(record)
                self._force_close()
                raise BrowserExecutorError("本机浏览器执行进程启动失败，发送通道未开启。") from error
            except Exception:
                self._force_close()
                raise
            finally:
                self._finish(record, timer, outcome)

    def connect(self) -> None:
        if self.is_connected:
            return
        began = time.monotonic()
        self.start(timeout_seconds=20.0)
        remaining = 20.0 - (time.monotonic() - began)
        if remaining <= 0:
            self._force_close()
            raise BrowserOperationTimeout("浏览器连接启动总期限已到。")
        self.call("connect", timeout_seconds=remaining)
        stale = list(self._owned_targets)
        if stale:
            remaining = 20.0 - (time.monotonic() - began)
            if remaining <= 0:
                self._force_close()
                raise BrowserOperationTimeout("浏览器连接与旧客服页清理超过总期限。")
            self.call("close_owned_targets", stale, timeout_seconds=min(8.0, remaining))
            self._owned_targets.difference_update(stale)
            self._persist_fence("connected")
        self._connected = True

    def call(
        self,
        operation: str,
        *args: Any,
        timeout_seconds: float | None = None,
        checkpoint: Callable[[str, dict[str, Any]], Any] | None = None,
        allow_stop: bool = False,
    ) -> Any:
        with self._call_lock:
            if self._connection is None:
                raise BrowserExecutorError("浏览器执行者不可用，需要先恢复。")
            timeout = timeout_seconds or OPERATION_TIMEOUTS.get(operation, 8.0)
            if operation == "open_conversation" and args:
                self._conversation_key = str(args[0])
            record, timer = self._begin(operation, timeout)
            outcome = "error"
            connection = self._connection
            callback_error: Exception | None = None
            try:
                self._check(record, allow_stop=allow_stop)
                connection.send_bytes(pack_message({
                    "kind": "command", "epoch": self._epoch,
                    "request_id": record["request_id"], "operation": operation,
                    "args": args, "deadline_seconds": timeout,
                }))
                while True:
                    self._check(record, allow_stop=allow_stop)
                    if not connection.poll(0.1):
                        continue
                    message = unpack_message(connection.recv_bytes(MAX_MESSAGE_BYTES))
                    self._check(record, allow_stop=allow_stop)
                    if (message.get("epoch") != record["epoch"]
                            or message.get("request_id") != record["request_id"]):
                        raise BrowserExecutorError("浏览器执行者返回过期或不匹配响应。")
                    kind = message.get("kind")
                    if kind == "target_created":
                        self._owned_targets.add(str(message["target_id"]))
                        self._persist_fence("running")
                        continue
                    if kind in {"checkpoint", "evidence"}:
                        action = str(message["action"])
                        if action not in self._evidence:
                            raise BrowserExecutorError("未知浏览器提交动作。")
                        evidence = dict(message.get("evidence") or {})
                        evidence.update(request_id=record["request_id"], epoch=record["epoch"],
                                        actor_id=record["actor_id"])
                        if evidence.get("conversation_key"):
                            record["conversation_key"] = str(evidence["conversation_key"])
                        if record["submission_authorized"]:
                            evidence["submission_authorized"] = True
                        self._evidence[action] = evidence
                        if kind == "evidence":
                            continue
                        try:
                            if checkpoint is None:
                                raise ChatAdapterError("缺少父进程提交检查，未批准提交。")
                            permit_id = checkpoint(action, evidence)
                            record["submission_authorized"] = True
                            evidence.update(submission_authorized=True,
                                            click_state="may_have_submitted")
                            if permit_id is not None:
                                evidence["permit_id"] = str(permit_id)
                                record["permit_id"] = str(permit_id)
                            self._evidence[action] = evidence
                            self._check(record, allow_stop=allow_stop)
                            connection.send_bytes(pack_message({
                                "kind": "permit", "epoch": record["epoch"],
                                "request_id": record["request_id"], "permit_id": permit_id,
                            }))
                        except BrowserExecutorError:
                            raise
                        except Exception as error:  # noqa: BLE001 - denied durable checkpoints never click
                            callback_error = error
                            connection.send_bytes(pack_message({
                                "kind": "denied", "epoch": record["epoch"],
                                "request_id": record["request_id"], "error": encode_error(error),
                            }))
                        continue
                    if kind == "error":
                        error = callback_error or decode_error(message["error"])
                        if record["submission_authorized"] and isinstance(
                            error, (TextSendNotPerformedError, PriceChangeNotPerformedError),
                        ):
                            error = ChatAdapterError("提交已获许可，结果需核对，禁止再次提交。")
                        raise error
                    if kind != "result":
                        raise BrowserExecutorError("无效浏览器执行响应。")
                    self._persist_fence("running")
                    if operation == "read_conversation":
                        result_key = getattr(message.get("result"), "conversation_key", None)
                        if result_key:
                            self._conversation_key = str(result_key)
                            record["conversation_key"] = self._conversation_key
                    outcome = "complete"
                    return message.get("result")
            except BrowserOperationTimeout:
                outcome = "timeout"
                raise
            except BrowserOperationCancelled:
                outcome = "cancelled"
                raise
            except (OSError, EOFError, ValueError, TypeError) as error:
                self._check(record, allow_stop=allow_stop)
                self._force_close()
                raise BrowserExecutorError(
                    "本机浏览器执行通道中断，已停止新增操作。", self._diagnostic(record),
                ) from error
            except BrowserExecutorError:
                self._force_close()
                raise
            finally:
                self._finish(record, timer, outcome)

    def recover(self) -> None:
        """Stop the old actor first, then close only registered stale app targets."""
        with self._call_lock:
            self._force_close()
            if self._cleanup_error:
                raise BrowserExecutorError("旧浏览器执行者退出未确认，已暂停恢复。")
            self.connect()

    def request_stop(self) -> None:
        self._stop_event.set()
        active = self._active
        if active is not None:
            self._cancelled.add(active["request_id"])
        threading.Thread(target=self._force_close, daemon=True,
                         name="browser-executor-stop").start()

    def _force_close(self) -> None:
        with self._resource_lock:
            self._connected = False
            self._conversation_key = None
            process, job = self._process, self._job
            connection, listener = self._connection, self._listener
            self._connection = None
            self._listener = None
            try:
                # Starting/running fences already identify the named Job and
                # authenticated actor. Termination must never wait for a fresh
                # fsync; keep that durable fence until exit is positively proved.
                if job is not None:
                    job.terminate()
                if process is not None and process.poll() is None:
                    process.kill()
                if process is not None:
                    process.wait(timeout=2.0)
                if job is not None:
                    deadline = time.monotonic() + 2.0
                    while job.active_processes():
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Browser executor descendants did not exit")
                        time.sleep(0.02)
                self._process = None
                self._job = None
                self._cleanup_error = None
                self._known_processes.clear()
                if self._owns_fence:
                    # Keep registered stale targets for the next application run,
                    # but mark retirement only after every owned process exited.
                    payload = load_retired_fence(self._fence_path)
                    if payload is not None and not self._owned_targets:
                        self._fence_path.unlink(missing_ok=True)
                        self._owns_fence = False
            except (OSError, subprocess.TimeoutExpired, TimeoutError, BrowserExecutorError) as error:
                self._cleanup_error = type(error).__name__
            finally:
                if connection is not None:
                    connection.close()
                if listener is not None:
                    listener.close()
                if job is not None and self._job is None:
                    job.close()
                if self._active is None:
                    self._last = {**self._last, "state": "stopped",
                                  "executor_exit_confirmed": self._cleanup_error is None}

    def close(self) -> None:
        with self._call_lock:
            try:
                if self._connection is not None and not self._stop_event.is_set():
                    targets = list(self._owned_targets)
                    if targets:
                        self.call("close_owned_targets", targets, timeout_seconds=2.0)
                        self._owned_targets.difference_update(targets)
                    self.call("close", timeout_seconds=2.0, allow_stop=True)
            except Exception:  # noqa: BLE001 - force cleanup also covers broken IPC
                logger.warning("浏览器执行者正常关闭失败，转为终止自有进程。")
            finally:
                self._force_close()
        if self._cleanup_error:
            raise BrowserExecutorError("浏览器执行者退出未确认，发送通道保持暂停。")

    disconnect = close

    def self_test(self) -> dict[str, Any]:
        """Verify hidden launch, pipe, codec and Job Object without connecting CDP."""
        try:
            self.start(self_test=True)
            return self.call("self_test")
        finally:
            self.close()

"""Durable, stage-aware sending shared by replies and scheduled follow-ups."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from xianyu_assistant import __version__
from xianyu_assistant.customer_service.models import SendReceipt
from xianyu_assistant.customer_service.privacy import PrivacyRedactor
from xianyu_assistant.customer_service.protocols import TextSendNotPerformedError
from xianyu_assistant.customer_service.send_checks import send_check_block_reason

logger = logging.getLogger(__name__)


def safe_error(value: str) -> str:
    """Remove credentials as well as customer identifiers from diagnostics."""
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:api[_ -]?key|access[_ -]?token|password|authorization)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]", value,
    )
    return PrivacyRedactor().redact(value)


@dataclass(frozen=True)
class SendOutcome:
    status: str
    receipt: SendReceipt | None = None
    outgoing_key: str | None = None
    reason: str = ""
    error_type: str = ""
    stage: str = ""
    retryable: bool = False
    traceback_text: str = ""
    diagnostic: dict[str, Any] = field(default_factory=dict)


def recheck_error_outcome(error: Exception, stage: str) -> SendOutcome:
    """Page errors remain read-only failures, with no storage-error inference."""
    detail = safe_error(str(error))[:900]
    stack = safe_error("".join(traceback.format_exception(error)))[-8000:]
    diagnostic = getattr(error, "diagnostic", {})
    logger.warning("CS_SEND_RECHECK_FAILURE stage=%s exception=%s detail=%s\n%s",
                   stage, type(error).__name__, detail, stack)
    return SendOutcome("recheck_failed", reason=detail, error_type=type(error).__name__,
                       stage=stage, traceback_text=stack,
                       diagnostic=dict(diagnostic) if isinstance(diagnostic, dict) else {})


class ReliableSender:
    """Never retry a page submission; persist evidence before the first click."""

    def __init__(self, adapter: Any, repository: Any, clock: Any, run_id: int | None):
        self.adapter = adapter
        self.repository = repository
        self.clock = clock
        self.run_id = run_id
        self._checkpoint_evidence: dict[str, Any] = {}
        database = getattr(repository, "database_path", None)
        self.journal_root = (
            Path(database).parent / "send-recovery" if database is not None else None
        )

    def _journal(self, send_id: str, payload: dict[str, Any]) -> None:
        if self.journal_root is None:
            return
        self.journal_root.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(send_id.encode()).hexdigest()
        target = self.journal_root / f"{name}.json"
        temporary = self.journal_root / f"{name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            for delay in (0, 0.02, 0.05, 0.1):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(temporary, target)
                    break
                except PermissionError:
                    if delay == 0.1:
                        raise
        finally:
            temporary.unlink(missing_ok=True)

    def _evidence(self) -> dict[str, Any]:
        getter = getattr(self.adapter, "send_evidence", None)
        try:
            evidence = dict(getter()) if callable(getter) else {}
        except Exception:  # noqa: BLE001 - a killed actor cannot supply submission evidence
            evidence = {}
        return {**self._checkpoint_evidence, **evidence}

    def execute(self, send_id: str, conversation_key: str) -> SendOutcome:
        row = self.repository.get_quality_send(send_id)
        if row is None:
            return SendOutcome("persistence_failed", reason="发送记录不存在，未发送。")
        if row["status"] not in {"prepared", "not_performed"}:
            # Includes a previous uncertain submission; never turn it into a retry.
            return SendOutcome("blocked", reason="该发送已提交或结果待核对，禁止重复发送。")
        submission_getter = getattr(self.repository, "get_submission", None)
        guard = submission_getter(send_id) if callable(submission_getter) else None
        if guard and (guard["permission_issued"] or guard["state"] not in {"prepared", "not_performed"}):
            return SendOutcome("blocked", reason="该发送已有持久提交凭据，禁止重新提交。")
        self._checkpoint_evidence = {}
        permission_issued = False
        attempt_id = uuid4().hex
        receipt: SendReceipt | None = None
        verified_key: str | None = None
        verified = False
        stage = "preflight"
        callback = getattr(self.adapter, "set_send_checkpoint", None)
        payload: dict[str, Any] = {
            "send_id": send_id, "conversation_key": conversation_key,
            "run_id": self.run_id, "attempt_id": attempt_id,
            "status": "prepared", "updated_at": self.clock.now().isoformat(),
            "permission_issued": False,
        }

        def checkpoint(evidence: dict[str, Any]) -> str:
            nonlocal stage, permission_issued
            contact_check = getattr(self.repository, "check_contact_submission", None)
            if callable(contact_check):
                reason = contact_check(send_id, evidence, observed_at=self.clock.now())
                if reason:
                    evidence.update(stage="contact_preflight", click_state="not_started")
                    raise TextSendNotPerformedError(reason)
            stage = "submitting"
            permit_id = uuid4().hex
            evidence.update(permit_id=permit_id)
            self._checkpoint_evidence = dict(evidence)
            payload.update(status="submitting", evidence=evidence,
                           updated_at=self.clock.now().isoformat())
            # Both writes must finish before the adapter performs any click.
            self._journal(send_id, payload)
            authorize = getattr(self.repository, "authorize_submission", None)
            if callable(authorize):
                authorize(action_id=send_id, action_type="text", conversation_key=conversation_key,
                          evidence=evidence, updated_at=self.clock.now(), run_id=self.run_id,
                          turn_fingerprint=row.get("turn_id"), attempt_id=attempt_id,
                          permit_id=permit_id)
            else:
                self.repository.update_send_attempt(
                    attempt_id, stage=stage, click_state="may_have_submitted",
                    evidence=evidence, updated_at=self.clock.now(),
                )
                self.repository.set_send_progress(
                    send_id, status="sending", stage=stage,
                    click_state="may_have_submitted", evidence=evidence,
                    updated_at=self.clock.now(),
                )
            permission_issued = True
            payload["permission_issued"] = True
            self._checkpoint_evidence["permission_issued"] = True
            return permit_id

        try:
            stage = "start_attempt"
            prepare = getattr(self.repository, "prepare_submission", None)
            if callable(prepare):
                prepare(action_id=send_id, action_type="text", conversation_key=conversation_key,
                        updated_at=self.clock.now(), run_id=self.run_id,
                        turn_fingerprint=row.get("turn_id"))
            if callable(callback):
                callback(checkpoint)
            self.repository.start_send_attempt(
                attempt_id=attempt_id, send_id=send_id, run_id=self.run_id,
                conversation_key=conversation_key, created_at=self.clock.now(),
                program_version=__version__,
            )
            if not callable(callback):
                checkpoint({})
            stage = "send_text"
            receipt = self.adapter.send_text(row["final_text"])
            evidence = self._evidence()
            stage = "persist_readback"
            payload.update(status="uncertain", evidence=evidence,
                           updated_at=self.clock.now().isoformat())
            self._journal(send_id, payload)
            self.repository.set_send_progress(
                send_id, status="sending", stage="readback",
                click_state=evidence.get("click_state", "may_have_submitted"),
                evidence=evidence, updated_at=self.clock.now(),
            )
            stage = "readback"
            if not self.adapter.verify_outgoing(receipt):
                raise RuntimeError("发送后回读未确认新增消息。")
            resolver = getattr(self.adapter, "verified_outgoing_message_key", None)
            verified_key = resolver(receipt) if callable(resolver) else receipt.message_key
            if not verified_key:
                raise RuntimeError("回读未取得实际消息标识。")
            verified = True
            stage = "persist_verified"
            payload.update(status="verified", outgoing_message_key=verified_key,
                           updated_at=self.clock.now().isoformat())
            self._journal(send_id, payload)
            self.repository.complete_reliable_send(
                send_id, outgoing_message_key=verified_key, updated_at=self.clock.now(),
            )
            self.repository.update_send_attempt(
                attempt_id, stage=stage, click_state="confirmed", result="verified",
                evidence=evidence, updated_at=self.clock.now(),
            )
            return SendOutcome("verified", receipt, verified_key)
        except Exception as error:  # noqa: BLE001 - every submission outcome must be preserved
            evidence = self._evidence()
            click = evidence.get("click_state", "may_have_submitted")
            # Only explicit pre-click evidence or the adapter's typed result is safe to retry.
            diagnostic = getattr(error, "diagnostic", {})
            authorized = permission_issued or bool(
                isinstance(diagnostic, dict) and diagnostic.get("submission_authorized"))
            actor_proved_no_permit = (isinstance(diagnostic, dict)
                                     and diagnostic.get("submission_authorized") is False)
            not_performed = (isinstance(error, TextSendNotPerformedError)
                             or actor_proved_no_permit) and not authorized
            status = "verified_unrecorded" if verified else (
                "not_performed" if not_performed else "uncertain"
            )
            underlying = error.__cause__ or error
            error_type = type(underlying).__name__
            actual_stage = str(evidence.get("stage") or stage)
            if stage in {"persist_verified", "persist_readback"}:
                actual_stage = stage
            if not verified and (actual_stage == "persist_submission" or stage in {
                "start_attempt", "persist_readback",
            }):
                status = "persistence_failed"
            detail = safe_error(str(error))[:900]
            stack = safe_error("".join(traceback.format_exception(error)))[-8000:]
            reason = f"阶段={actual_stage}；异常={error_type}；{detail}"
            logger.error(
                "CS_SEND_FAILURE attempt=%s stage=%s click=%s result=%s exception=%s detail=%s\n%s",
                attempt_id, actual_stage, click, status, error_type, detail, stack,
            )
            payload.update(status=status, evidence=evidence, failure_reason=reason,
                           updated_at=self.clock.now().isoformat())
            if verified_key:
                payload["outgoing_message_key"] = verified_key
            try:
                self._journal(send_id, payload)
                self.repository.set_send_progress(
                    send_id, status="verified" if verified else status,
                    stage=actual_stage, click_state=click, evidence=evidence,
                    failure_reason=reason, updated_at=self.clock.now(),
                    next_check_at=(self.clock.now() + timedelta(seconds=15))
                    if status == "uncertain" and evidence.get("baseline_keys") else None,
                )
                self.repository.update_send_attempt(
                    attempt_id, stage=actual_stage, click_state=click, result=status,
                    exception_type=error_type, exception_detail=detail,
                    traceback_text=stack, evidence=evidence, updated_at=self.clock.now(),
                )
                finish = getattr(self.repository, "finish_submission", None)
                if callable(finish) and not verified:
                    finish(send_id, state="not_performed" if not_performed else (
                        "persistence_failed" if status == "persistence_failed" else "uncertain"),
                        evidence=evidence, failure_code=error_type, updated_at=self.clock.now(),
                        proved_not_performed=not_performed)
            except Exception as storage_error:  # noqa: BLE001 - never continue unaudited sends
                logger.error("CS_SEND_AUDIT_FAILURE %s", safe_error(str(storage_error))[:500])
                return SendOutcome(
                    "persistence_failed", receipt, verified_key,
                    "发送诊断无法保存，已停止新增发送；本条禁止自动重发。",
                    type(storage_error).__name__, actual_stage,
                )
            retryable = not_performed and bool(getattr(error, "retryable", False))
            return SendOutcome(status, receipt, verified_key, reason,
                               error_type, actual_stage, retryable)
        finally:
            if callable(callback):
                callback(None)

    def reconcile(self, row: dict[str, Any]) -> SendOutcome:
        """Inspect an earlier submission once; never fill or click an editor."""
        reason = send_check_block_reason(row, self.clock.now())
        if reason:
            return SendOutcome("manual_required", reason=reason, stage="eligibility")
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
            reconcile = getattr(self.adapter, "reconcile_send", None)
            if not callable(reconcile) or not evidence.get("baseline_keys"):
                return SendOutcome("manual_required", reason="缺少提交基线，需人工核对。")
            status, key = reconcile(evidence, row["reply_fingerprint"])
        except Exception as error:  # noqa: BLE001 - this boundary performs only DOM reads
            return recheck_error_outcome(error, "deferred_readback")
        if status == "verified" and key:
            try:
                self._journal(row["send_id"], {
                    "send_id": row["send_id"], "status": "verified",
                    "outgoing_message_key": key, "evidence": evidence,
                    "updated_at": self.clock.now().isoformat(),
                })
                self.repository.complete_reliable_send(
                    row["send_id"], outgoing_message_key=key, updated_at=self.clock.now(),
                )
                return SendOutcome("verified", SendReceipt(key, row["reply_fingerprint"]), key)
            except Exception as error:  # noqa: BLE001 - confirmed evidence writes fail closed
                outcome = recheck_error_outcome(error, "persist_recheck_verified")
                return SendOutcome("persistence_failed", outgoing_key=key, reason=outcome.reason,
                                   error_type=outcome.error_type, stage=outcome.stage,
                                   traceback_text=outcome.traceback_text)
        return SendOutcome(status, reason="同文消息已出现，来源仍需人工核对。"
                           if status == "observed_unattributed" else "本次回读仍未确认。",
                           stage="deferred_readback")

    def recover_journals(self) -> None:
        """Import durable evidence after obtaining exclusive reception ownership."""
        if self.journal_root is None or not self.journal_root.is_dir():
            return
        for path in self.journal_root.glob("*.json"):
            if path.stat().st_size > 1_000_000:
                raise ValueError("发送恢复凭据过大，已停止新增发送，请检查本机凭据。")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("send_id"), str):
                    raise TypeError("invalid recovery schema")
                if path.stem != hashlib.sha256(payload["send_id"].encode()).hexdigest():
                    raise ValueError("recovery identity mismatch")
            except (ValueError, TypeError, OSError) as error:
                raise ValueError("发送恢复凭据损坏，已停止新增发送，需人工检查。") from error
            row = self.repository.get_quality_send(payload["send_id"])
            if row is None:
                guard_getter = getattr(self.repository, "get_submission", None)
                guard = guard_getter(payload["send_id"]) if callable(guard_getter) else None
                if guard and guard["state"] in {"verified", "not_performed"}:
                    path.unlink(missing_ok=True)
                    continue
                raise ValueError("恢复凭据缺少原发送记录，禁止忽略孤儿凭据后继续发送。")
            if payload.get("conversation_key", row["conversation_key"]) != row["conversation_key"]:
                raise ValueError("发送恢复凭据会话不一致，已停止新增发送。")
            evidence = payload.get("evidence", {})
            if not isinstance(evidence, dict):
                raise TypeError("发送恢复证据格式无效，已停止新增发送。")
            guard_getter = getattr(self.repository, "get_submission", None)
            stored_guard = guard_getter(row["send_id"]) if callable(guard_getter) else None
            if stored_guard and stored_guard["state"] in {"manual_resolved", "not_performed"}:
                path.unlink(missing_ok=True)
                continue
            permitted = payload.get("status") in {
                "submitting", "uncertain", "persistence_failed", "verified_unrecorded", "verified",
            } and (payload.get("permission_issued", True) or bool(
                stored_guard and stored_guard["permission_issued"]))
            restore = getattr(self.repository, "restore_submission_guard", None)
            if permitted and callable(restore):
                restore(action_id=row["send_id"], action_type="text",
                        conversation_key=row["conversation_key"], evidence=evidence,
                        turn_fingerprint=row.get("turn_id"), updated_at=self.clock.now(),
                        run_id=self.run_id)
            if payload.get("status") == "verified" and payload.get("outgoing_message_key"):
                if row["status"] != "verified" or not row.get("outgoing_message_key"):
                    self.repository.complete_reliable_send(
                        row["send_id"], outgoing_message_key=payload["outgoing_message_key"],
                        updated_at=self.clock.now(),
                    )
            elif row["status"] != "verified" and permitted:
                self.repository.set_send_progress(
                    row["send_id"], status="uncertain", stage="restart_recovery",
                    click_state="may_have_submitted", evidence=evidence,
                    failure_reason="程序在提交阶段中断，禁止自动重发，需核对。",
                    updated_at=self.clock.now(), next_check_at=self.clock.now(),
                )
            # Keep only unexpired evidence; completed sends are durable in the database.
            if row["status"] == "verified":
                path.unlink(missing_ok=True)

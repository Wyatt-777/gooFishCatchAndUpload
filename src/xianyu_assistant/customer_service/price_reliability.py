"""Single-permit order-price changes with durable, read-only recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from xianyu_assistant.customer_service.checkout import (
    approved_binding_matches,
    reliable_order_binding,
)
from xianyu_assistant.customer_service.models import PriceChangeReceipt
from xianyu_assistant.customer_service.protocols import PriceChangeNotPerformedError
from xianyu_assistant.customer_service.send_reliability import ReliableSender, safe_error


@dataclass(frozen=True)
class PriceOutcome:
    status: str
    receipt: PriceChangeReceipt | None = None
    reason: str = ""
    stage: str = ""
    retryable: bool = False


class ReliablePriceChanger(ReliableSender):
    """Require a parent commit before the irreversible second confirmation."""

    def __init__(self, adapter: Any, repository: Any, clock: Any, run_id: int | None):
        super().__init__(adapter, repository, clock, run_id)
        database = getattr(repository, "database_path", None)
        self.journal_root = Path(database).parent / "price-recovery" if database else None

    def _evidence(self) -> dict[str, Any]:
        getter = getattr(self.adapter, "price_evidence", None)
        try:
            evidence = dict(getter()) if callable(getter) else {}
        except Exception:  # noqa: BLE001 - a terminated actor cannot supply evidence
            evidence = {}
        return {**self._checkpoint_evidence, **evidence}

    def execute(self, task_id: str, conversation_key: str, approved_price: str,
                *, expected_binding: dict[str, Any] | None = None) -> PriceOutcome:
        guard = self.repository.get_submission(task_id)
        if guard and (guard["permission_issued"] or guard["state"] not in {"prepared", "not_performed"}):
            return PriceOutcome("blocked", reason="该改价已有提交许可，禁止再次确认。")
        callback = getattr(self.adapter, "set_price_checkpoint", None)
        verifier = getattr(self.adapter, "verify_order_price", None)
        if not callable(callback) or not callable(verifier):
            return PriceOutcome("not_performed", reason="订单改价缺少提交凭据或订单金额回读，未提交。")
        capability = getattr(self.adapter, "can_verify_order_price", None)
        if callable(capability) and not capability():
            return PriceOutcome("not_performed", reason="未配置实际订单金额及运费回读，未执行改价。")
        self._checkpoint_evidence = {}
        permission_issued = False
        stage = "prepare"
        receipt = None
        verified = False
        payload: dict[str, Any] = {
            "action_id": task_id, "action_type": "price", "conversation_key": conversation_key,
            "approved_price": approved_price, "run_id": self.run_id,
            "status": "prepared", "permission_issued": False,
            "updated_at": self.clock.now().isoformat(),
        }

        def checkpoint(evidence: dict[str, Any]) -> str:
            nonlocal permission_issued, stage
            stage = "persist_submission"
            if expected_binding and expected_binding.get("checkout_send_id"):
                evidence["checkout_send_id"] = expected_binding["checkout_send_id"]
            if str(evidence.get("approved_price")) != approved_price:
                raise PriceChangeNotPerformedError("最终确认金额与已批准金额不一致，未提交。")
            if not all(evidence.get(key) for key in (
                "merchant_account_id", "platform_customer_id", "product_id",
                "shipping_price", "last_customer_key", "submitted_at",
            )) or evidence.get("conversation_key") != conversation_key or not reliable_order_binding(evidence):
                raise PriceChangeNotPerformedError("改价缺少可靠订单或会话身份，未提交最终确认。")
            if evidence.get("order_price_verifiable") is False:
                raise PriceChangeNotPerformedError("订单金额回读不可用，未执行最终改价。")
            if expected_binding is not None and not approved_binding_matches(evidence, expected_binding):
                raise PriceChangeNotPerformedError("最终提交身份或消息与批准任务不一致，未改价。")
            permit_id = uuid4().hex
            evidence["permit_id"] = permit_id
            self._checkpoint_evidence = dict(evidence)
            payload.update(status="submitting", evidence=evidence,
                           updated_at=self.clock.now().isoformat())
            self._journal(task_id, payload)
            self.repository.authorize_submission(
                action_id=task_id, action_type="price", conversation_key=conversation_key,
                evidence=evidence, updated_at=self.clock.now(), run_id=self.run_id,
                permit_id=permit_id,
            )
            permission_issued = True
            payload["permission_issued"] = True
            self._checkpoint_evidence["permission_issued"] = True
            stage = "final_confirm"
            return permit_id

        try:
            self.repository.prepare_submission(action_id=task_id, action_type="price",
                                               conversation_key=conversation_key,
                                               updated_at=self.clock.now(), run_id=self.run_id)
            callback(checkpoint)
            stage = "open_price_dialog"
            receipt = self.adapter.change_order_price(approved_price)
            if not permission_issued:
                raise RuntimeError("页面返回改价结果但没有最终确认提交许可。")
            evidence = self._evidence()
            payload.update(status="uncertain", evidence=evidence,
                           updated_at=self.clock.now().isoformat())
            self._journal(task_id, payload)
            stage = "order_price_readback"
            if not verifier(evidence):
                raise RuntimeError("未确认同一订单的目标金额及运费，禁止重提改价。")
            readback = self._evidence().get("order_readback")
            if readback:
                evidence["order_readback"] = readback
            verified = True
            stage = "persist_verified"
            payload.update(status="verified", evidence=evidence, updated_at=self.clock.now().isoformat())
            self._journal(task_id, payload)
            self.repository.finish_submission(task_id, state="verified", evidence=evidence,
                                              updated_at=self.clock.now())
            return PriceOutcome("verified", receipt, stage=stage)
        except Exception as error:  # noqa: BLE001 - every financial submission must retain its outcome
            diagnostic = getattr(error, "diagnostic", {})
            authorized = permission_issued or bool(isinstance(diagnostic, dict)
                                                  and diagnostic.get("submission_authorized"))
            actor_proved_no_permit = (isinstance(diagnostic, dict)
                                     and diagnostic.get("submission_authorized") is False)
            not_performed = (isinstance(error, PriceChangeNotPerformedError)
                             or actor_proved_no_permit) and not authorized
            storage_failure = stage in {"prepare", "persist_submission", "persist_verified"} and not not_performed
            state = "not_performed" if not_performed else (
                "persistence_failed" if storage_failure else "uncertain")
            reason = safe_error(str(error))[:900]
            evidence = self._evidence()
            payload.update(status="verified" if verified else state, evidence=evidence, failure_reason=reason,
                           updated_at=self.clock.now().isoformat())
            try:
                self._journal(task_id, payload)
                self.repository.finish_submission(task_id, state="verified" if verified else state, evidence=evidence,
                    failure_code=type(error).__name__, updated_at=self.clock.now(),
                    proved_not_performed=not_performed)
            except Exception as storage_error:  # noqa: BLE001 - fail closed on any audit failure
                return PriceOutcome("persistence_failed", receipt,
                    "改价证据无法保存，已停止新增发送；禁止重提：" + safe_error(str(storage_error))[:400], stage)
            return PriceOutcome(state, receipt, reason, stage, not_performed)
        finally:
            callback(None)

    def reconcile(self, action_id: str) -> PriceOutcome:
        """A matching price is an observation, never permission for another click."""
        row = self.repository.get_submission(action_id)
        if row is None or row["action_type"] != "price":
            return PriceOutcome("manual_required", reason="缺少改价恢复凭据。")
        if row["state"] == "verified":
            return PriceOutcome("verified")
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
            if not reliable_order_binding(evidence):
                return PriceOutcome("manual_required", reason="缺少可靠订单标识，需人工核对。")
            self.adapter.open_conversation(row["conversation_key"])
            if self.adapter.verify_order_price(evidence):
                return PriceOutcome("observed_unattributed", reason="同一订单显示目标金额，操作来源需人工核对。")
            return PriceOutcome("uncertain", reason="订单金额尚未确认，禁止重提。")
        except Exception as error:  # noqa: BLE001 - read-only recovery must never submit
            return PriceOutcome("recheck_failed", reason=safe_error(str(error))[:900], stage="price_recheck")

    def recover_journals(self) -> None:
        if self.journal_root is None or not self.journal_root.is_dir():
            return
        for path in self.journal_root.glob("*.json"):
            try:
                if path.stat().st_size > 1_000_000:
                    raise ValueError("oversized price journal")
                payload = json.loads(path.read_text(encoding="utf-8"))
                identifier = payload["action_id"]
                if (not isinstance(identifier, str) or payload.get("action_type") != "price"
                        or path.stem != hashlib.sha256(identifier.encode()).hexdigest()):
                    raise ValueError("invalid price recovery identity")
                evidence = payload.get("evidence", {})
                if not isinstance(evidence, dict):
                    raise TypeError("invalid price evidence")
                row = self.repository.get_submission(identifier)
                if row is None:
                    raise ValueError("orphan price recovery journal")
                if row["conversation_key"] != payload.get("conversation_key"):
                    raise ValueError("price recovery conversation mismatch")
                if row["state"] in {"verified", "not_performed", "manual_resolved"}:
                    path.unlink(missing_ok=True)
                    continue
                if payload.get("status") == "verified":
                    self.repository.finish_submission(identifier, state="verified", evidence=evidence,
                                                      updated_at=self.clock.now())
                    continue
                if payload.get("permission_issued") or row["permission_issued"]:
                    self.repository.restore_submission_guard(action_id=identifier, action_type="price",
                        conversation_key=row["conversation_key"], evidence=evidence,
                        updated_at=self.clock.now(), run_id=self.run_id)
            except (ValueError, TypeError, KeyError, OSError) as error:
                raise ValueError("改价恢复凭据损坏或缺失，已停止新增发送，需人工检查。") from error

"""Offline SQLite-copy and state checks; never create browser or model clients."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import zipfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xianyu_assistant.customer_service.clock import SystemClock
from xianyu_assistant.customer_service.price_reliability import ReliablePriceChanger
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.customer_service.send_reliability import ReliableSender
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def preserved_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], str, int]:
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    if not columns:
        return [], "", 0
    quoted = ",".join(f'"{name}"' for name in columns)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid'):
        digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
        digest.update(b"\n")
        count += 1
    return columns, digest.hexdigest(), count


def check_same_rows(connection: sqlite3.Connection, table: str,
                    before: tuple[list[str], str, int]) -> bool:
    columns, expected, expected_count = before
    quoted = ",".join(f'"{name}"' for name in columns)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid'):
        digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
        digest.update(b"\n")
        count += 1
    return count == expected_count and digest.hexdigest() == expected


def copy_database(source_path: Path, copy_path: Path) -> None:
    if source_path.resolve() == copy_path.resolve():
        raise ValueError("验证副本不能覆盖原数据库。")
    started = time.monotonic()

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() - started > 30:
            raise TimeoutError("只读数据库副本超出30秒预算。")

    with (
        sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as source,
        sqlite3.connect(copy_path, timeout=1) as target,
    ):
        source.backup(target, pages=256, progress=progress, sleep=0.05)


def require(condition: bool, name: str, checks: dict[str, bool]) -> None:
    checks[name] = condition
    if not condition:
        raise AssertionError(name)


def state_checks(directory: Path) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    repository = CustomerServiceRepository(directory / "state-check.sqlite3", busy_timeout_seconds=0.1)
    repository.initialize()
    clock = SystemClock()
    now = clock.now()
    old = now - timedelta(days=100)
    evidence = {"conversation_key": "state-conversation", "baseline_keys": ["state-key"],
                "last_customer_key": "state-customer-key", "submitted_at": old.isoformat(),
                "request_id": "state-request", "epoch": 1, "actor_id": "state-actor"}
    repository.prepare_submission(action_id="state-text", action_type="text",
                                  conversation_key="state-conversation", updated_at=old,
                                  turn_fingerprint="state-turn")
    repository.authorize_submission(action_id="state-text", action_type="text",
                                    conversation_key="state-conversation", evidence=evidence,
                                    updated_at=old, turn_fingerprint="state-turn")
    with repository._connection() as connection:
        connection.execute("""INSERT INTO cs_handoff_events
            (conversation_key,reason,status,handoff_type,release_on_resolve,created_at,content_expires_at,related_send_id)
            VALUES ('state-conversation','offline-state','open','technical_send',1,?,?,'state-text')""",
            (old.isoformat(), old.isoformat()))
    try:
        repository.authorize_submission(action_id="state-text", action_type="text",
                                        conversation_key="state-conversation", evidence=evidence,
                                        updated_at=now, turn_fingerprint="state-turn")
    except ValueError:
        require(True, "permission_cannot_be_issued_twice", checks)
    else:
        require(False, "permission_cannot_be_issued_twice", checks)
    try:
        repository.finish_submission("state-text", state="not_performed", updated_at=now,
                                     proved_not_performed=True)
    except ValueError:
        require(True, "permission_cannot_be_downgraded_by_no_click_claim", checks)
    else:
        require(False, "permission_cannot_be_downgraded_by_no_click_claim", checks)
    repository.cleanup_expired_audit(now=now)
    guard = repository.get_submission("state-text")
    require(bool(guard and guard["permission_issued"] and guard["evidence_json"] is None),
            "ttl_keeps_minimum_submission_guard", checks)
    require(repository.has_unresolved_submission("state-conversation"), "ttl_keeps_unresolved_lock", checks)
    require(any(event.conversation_key == "state-conversation" for event in repository.list_open_handoff_events()),
            "ttl_keeps_minimum_manual_recovery_notification", checks)
    try:
        repository.authorize_submission(action_id="new-text", action_type="text",
                                        conversation_key="state-conversation", evidence=evidence,
                                        updated_at=now, turn_fingerprint="new-turn")
    except ValueError:
        require(True, "new_action_cannot_bypass_unresolved_conversation", checks)
    else:
        require(False, "new_action_cannot_bypass_unresolved_conversation", checks)

    with repository._connection() as connection:
        connection.execute("""INSERT INTO cs_reply_jobs
            (job_id,conversation_key,batch_fingerprint,status,draft_text,created_at,updated_at)
            VALUES ('state-reading','reading-conversation','reading-turn','reading_context','',?,?)""",
            (now.isoformat(), now.isoformat()))
        connection.execute("""INSERT INTO cs_reply_jobs
            (job_id,conversation_key,batch_fingerprint,status,draft_text,created_at,updated_at)
            VALUES ('state-before-permit','pre-permit-conversation','pre-permit-turn','sending','',?,?)""",
            (now.isoformat(), now.isoformat()))
    repository.prepare_submission(action_id="state-before-permit", action_type="text",
                                  conversation_key="pre-permit-conversation", updated_at=now,
                                  turn_fingerprint="pre-permit-turn")
    repository.recover_interrupted_reply_jobs(recovered_at=now)
    pending = repository.get_reply_job_recovery("state-reading")
    require(bool(pending and pending["failure_code"] == "interrupted_before_submission"
                 and pending["recovery_state"] == "recovery_pending"),
            "pre_permission_job_has_explicit_recovery_code", checks)
    repository.resolve_recovery_pending_job("state-reading", updated_at=now)
    pre_permit = repository.get_reply_job_recovery("state-before-permit")
    require(bool(pre_permit and pre_permit["recovery_state"] == "recovery_pending"
                 and not repository.has_unresolved_submission("pre-permit-conversation")),
            "pre_permission_sending_job_requeues_without_unresolved_lock", checks)
    repository.resolve_recovery_pending_job("state-before-permit", updated_at=now)
    require(not repository.list_recovery_pending_jobs(), "recovery_pending_can_be_resolved", checks)

    # The only adapter is an object with no browser methods. The boundary must
    # refuse the operation before persistence permission or page interaction.
    outcome = ReliablePriceChanger(object(), repository, clock, None).execute(
        "missing-price-capability", "capability-conversation", "1.00")
    require(outcome.status == "not_performed" and repository.get_submission("missing-price-capability") is None,
            "missing_price_capability_refuses_before_permission", checks)
    price_evidence = {
        "conversation_key": "price-state-conversation", "merchant_account_id": "state-merchant",
        "platform_customer_id": "state-buyer", "product_id": "state-product", "order_id": "state-order",
        "approved_price": "1.00", "shipping_price": "0.00", "last_customer_key": "state-customer",
        "submitted_at": old.isoformat(), "order_price_verifiable": False,
    }
    repository.prepare_submission(action_id="price-state-action", action_type="price",
                                  conversation_key="price-state-conversation", updated_at=old)
    try:
        repository.authorize_submission(action_id="price-state-action", action_type="price",
                                        conversation_key="price-state-conversation", evidence=price_evidence,
                                        updated_at=old)
    except ValueError:
        require(not repository.get_submission("price-state-action")["permission_issued"],
                "unverifiable_order_price_cannot_receive_permission", checks)
    else:
        require(False, "unverifiable_order_price_cannot_receive_permission", checks)
    price_evidence["order_price_verifiable"] = True
    repository.authorize_submission(action_id="price-state-action", action_type="price",
                                    conversation_key="price-state-conversation", evidence=price_evidence,
                                    updated_at=old)
    repository.finish_submission("price-state-action", state="verified", updated_at=old)
    repository.cleanup_expired_audit(now=now)
    try:
        repository.authorize_submission(action_id="new-price-action", action_type="price",
                                        conversation_key="price-state-conversation", evidence=price_evidence,
                                        updated_at=now)
    except ValueError:
        require(True, "ttl_keeps_completed_order_price_deduplication", checks)
    else:
        require(False, "ttl_keeps_completed_order_price_deduplication", checks)

    journal_root = directory / "send-recovery"
    journal_root.mkdir(exist_ok=True)
    orphan_id = "offline-orphan"
    orphan_path = journal_root / (hashlib.sha256(orphan_id.encode()).hexdigest() + ".json")
    orphan_path.write_text(json.dumps({"send_id": orphan_id, "status": "submitting",
                                       "updated_at": now.isoformat()}), encoding="utf-8")
    try:
        ReliableSender(object(), repository, clock, None).recover_journals()
    except ValueError:
        require(True, "orphan_submission_journal_fails_closed", checks)
    else:
        require(False, "orphan_submission_journal_fails_closed", checks)
    orphan_path.unlink()
    bad_path = journal_root / "invalid.json"
    bad_path.write_text("{", encoding="utf-8")
    try:
        ReliableSender(object(), repository, clock, None).recover_journals()
    except ValueError:
        require(True, "corrupt_submission_journal_fails_closed", checks)
    else:
        require(False, "corrupt_submission_journal_fails_closed", checks)
    bad_path.unlink()

    blocker = sqlite3.connect(repository.database_path, timeout=0.1)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            repository.prepare_submission(action_id="busy-action", action_type="text",
                                          conversation_key="busy-conversation", updated_at=now)
        except sqlite3.OperationalError:
            require(time.monotonic() - started < 1, "sqlite_busy_wait_is_bounded", checks)
        else:
            require(False, "sqlite_busy_wait_is_bounded", checks)
    finally:
        blocker.rollback()
        blocker.close()
    require(repository.get_submission("busy-action") is None, "busy_write_issued_no_permission", checks)

    run_id = repository.start_run_session(mode="offline_validation", started_at=now)
    repository.save_run_runtime_config(run_id, settings={
        "mode": "offline_validation", "sqlite_busy_timeout_seconds": repository.busy_timeout_seconds,
        "browser_connected": False, "model_connected": False, "api_key": "unused-validation-value",
    }, recorded_at=now)
    repository.finish_run_session(run_id, status="stopped", stopped_at=now,
                                  recovery_state="offline_validation_complete")
    repository.prepare_submission(action_id="export-state-action", action_type="text",
                                  conversation_key="export-state-conversation", updated_at=now,
                                  turn_fingerprint="export-state-turn")
    repository.authorize_submission(action_id="export-state-action", action_type="text",
        conversation_key="export-state-conversation", evidence={
            **evidence, "conversation_key": "export-state-conversation", "submitted_at": now.isoformat()},
        updated_at=now, turn_fingerprint="export-state-turn")
    bundle_path = directory / "state-runtime.zip"
    export_quality_bundle(repository.database_path, bundle_path, now=now)
    with zipfile.ZipFile(bundle_path) as bundle:
        runtime = json.loads(bundle.read("runtime.json"))
        settings = runtime["runs"][0]["settings_json"]
        require(settings["sqlite_busy_timeout_seconds"] == repository.busy_timeout_seconds,
                "export_runtime_uses_recorded_settings", checks)
        require(settings["api_key"] == "[REDACTED]", "export_runtime_redacts_credentials", checks)
        guard_lines = [json.loads(line) for line in bundle.read("submission_tombstones.jsonl").splitlines()]
        require(bool(guard_lines) and all(
            str(line.get("conversation_key")).startswith("conv-")
            and str(line.get("action_id")).startswith("action-") for line in guard_lines),
                "export_submission_identifiers_are_pseudonyms", checks)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(os.environ["LOCALAPPDATA"])
                        / "XianyuAssistant" / "xianyu_assistant.db")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-state-checks", action="store_true",
                        help="只检查真实数据库副本迁移及导出，不重复纯状态检查。")
    parser.add_argument("--runtime-schema-only", action="store_true",
                        help="隔离新增运行恢复schema，跳过以前版本的商品事实资料迁移。")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    copy_path = args.output_dir / "source-copy.sqlite3"
    report: dict[str, object] = {
        "source": str(args.source.resolve()), "copy": str(copy_path.resolve()),
        "browser_started": False, "llm_started": False, "checks": {},
        "runtime_schema_only": args.runtime_schema_only,
    }
    try:
        if copy_path.exists() or (args.output_dir / "state-check.sqlite3").exists():
            raise ValueError("请选择新的验证输出目录，避免覆盖已有验证副本。")
        copy_database(args.source, copy_path)
        tables = ("cs_products", "cs_settings", "cs_messages", "cs_quality_messages")
        with sqlite3.connect(copy_path) as connection:
            before = {table: preserved_rows(connection, table) for table in tables}
        repository = CustomerServiceRepository(copy_path)
        repository.initialize(migrate_confirmed_facts=not args.runtime_schema_only)
        repository.initialize(migrate_confirmed_facts=not args.runtime_schema_only)
        checks: dict[str, bool] = {}
        with sqlite3.connect(copy_path) as connection:
            require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "real_copy_integrity_after_migration", checks)
            require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                    "real_copy_foreign_keys_after_migration", checks)
            for table, snapshot in before.items():
                require(check_same_rows(connection, table, snapshot), f"migration_preserves_{table}", checks)
        bundle_path = args.output_dir / "real-copy-quality.zip"
        export_quality_bundle(copy_path, bundle_path)
        required = {"runtime_configs.jsonl", "browser_operations.jsonl", "runtime_recoveries.jsonl",
                    "submission_tombstones.jsonl", "price_recovery.jsonl", "runtime.json"}
        with zipfile.ZipFile(bundle_path) as bundle:
            require(required.issubset(bundle.namelist()), "real_copy_export_contains_runtime_files", checks)
            runtime = json.loads(bundle.read("runtime.json"))
            require("readback_seconds" not in runtime and "scan_budget_seconds" not in runtime,
                    "legacy_export_does_not_invent_actual_parameters", checks)
        if not args.skip_state_checks:
            state_directory = args.output_dir / "pure-state"
            state_directory.mkdir()
            checks.update(state_checks(state_directory))
        report.update(success=True, checks=checks, preserved_row_counts={
            table: values[2] for table, values in before.items()}, bundle=str(bundle_path.resolve()))
        exit_code = 0
    except (AssertionError, ValueError, OSError, sqlite3.Error, TimeoutError) as error:
        report.update(success=False, error_type=type(error).__name__, error=str(error))
        exit_code = 1
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report.get("success"), "report": str(report_path.resolve()),
                      "check_count": len(report.get("checks", {})), "error": report.get("error")},
                     ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

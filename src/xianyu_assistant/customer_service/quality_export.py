"""Read-only, privacy-aware export of real reception conversations."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xianyu_assistant import __version__
from xianyu_assistant.customer_service.contact_policy import CONTACT_POLICY_VERSION
from xianyu_assistant.customer_service.fact_guard import GUARD_VERSION
from xianyu_assistant.customer_service.privacy import PrivacyRedactor
from xianyu_assistant.customer_service.send_reliability import safe_error
from xianyu_assistant.persistence.recontact_storage import RECONTACT_POLICY_VERSION

SCHEMA_VERSION = 4
_SOURCE_LABELS = {
    "customer": "顾客",
    "application_auto": "商家 · 应用自动回复",
    "application_reviewed": "商家 · 应用内人工审核回复",
    "merchant_external": "商家 · 非应用发送（是否人工未知）",
    "merchant_source_uncertain": "商家 · 来源待确认",
    "platform_system": "平台系统提示",
    "role_uncertain": "消息角色待确认",
}


def export_quality_bundle(
    database_path: Path,
    output_path: Path,
    *,
    days: int = 30,
    include_raw_text: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Write one consistent ZIP snapshot without changing the live database."""
    if days not in {7, 30, 90}:
        raise ValueError("导出范围只能是最近 7、30 或 90 天。")
    if output_path.resolve() == database_path.resolve():
        raise ValueError("导出文件不能覆盖客服数据库。")
    current = now or datetime.now().astimezone()
    cutoff = (current - timedelta(days=days)).isoformat(timespec="seconds")
    if not database_path.is_file():
        raise FileNotFoundError("找不到客服数据库。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(32)
    redactor = PrivacyRedactor()
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    export_deadline = time.monotonic() + 120

    def check_export_deadline() -> None:
        if time.monotonic() > export_deadline:
            raise TimeoutError("质检导出超过120秒预算，原数据库未修改，请缩小导出范围。")

    def ref(kind: str, value: object | None) -> str | None:
        if value is None:
            return None
        digest = hmac.new(salt, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
        return f"{kind}-{digest[:20]}"

    def clean(value: object) -> object:
        if include_raw_text:
            return value
        if isinstance(value, str):
            return redactor.redact(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    def write_text(archive: zipfile.ZipFile, name: str, value: str) -> None:
        payload = value.encode("utf-8-sig" if name.endswith(".csv") else "utf-8")
        archive.writestr(name, payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()

    def write_lines(archive: zipfile.ZipFile, name: str, lines: Any) -> int:
        digest = hashlib.sha256()
        count = 0
        with archive.open(name, "w") as stream:
            for line in lines:
                if count % 128 == 0:
                    check_export_deadline()
                payload = (line + "\n").encode()
                stream.write(payload)
                digest.update(payload)
                count += 1
        hashes[name] = digest.hexdigest()
        return count

    uri = f"file:{database_path.as_posix()}?mode=ro"
    connection: sqlite3.Connection | None = None
    snapshot_path: Path | None = None
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="quality-snapshot-", suffix=".sqlite3", delete=False,
        ) as temporary_snapshot:
            snapshot_path = Path(temporary_snapshot.name)
        source = sqlite3.connect(uri, uri=True, timeout=5)
        backup_deadline = time.monotonic() + 30

        def backup_progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > backup_deadline:
                raise TimeoutError("数据库快照超过30秒预算，导出已停止，原数据库未修改。")

        try:
            snapshot = sqlite3.connect(snapshot_path)
            try:
                source.backup(snapshot, pages=256, sleep=0.05, progress=backup_progress)
            finally:
                snapshot.close()
        finally:
            source.close()
        connection = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(lambda: int(time.monotonic() > export_deadline), 10_000)
        with tempfile.NamedTemporaryFile(
            prefix="quality-export-", suffix=".zip", dir=output_path.parent,
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            definitions = (
                ("messages", "cs_quality_messages", "last_seen_at"),
                ("sends", "cs_quality_sends", "created_at"),
                ("decisions", "cs_quality_decisions", "created_at"),
                ("events", "cs_quality_events", "created_at"),
                ("runs", "cs_run_sessions", "started_at"),
                ("reply_jobs", "cs_reply_jobs", "updated_at"),
                ("handoffs", "cs_handoff_events", "created_at"),
                ("price_changes", "cs_price_change_tasks", "updated_at"),
                ("send_outbox", "cs_send_outbox", "updated_at"),
                ("send_attempts", "cs_send_attempts", "updated_at"),
                ("send_checks", "cs_send_checks", "updated_at"),
                ("contact_guards", "cs_contact_guards", "updated_at"),
                ("contact_bindings", "cs_contact_bindings", "updated_at"),
                ("contact_activity", "cs_contact_activity", "updated_at"),
                ("contact_seen_messages", "cs_contact_seen_messages", "observed_at"),
                ("recontact_windows", "cs_recontact_windows", "updated_at"),
                ("order_observations", "cs_order_observations", "created_at"),
                ("runtime_configs", "cs_runtime_configs", "updated_at"),
                ("browser_operations", "cs_browser_operations", "started_at"),
                ("runtime_recoveries", "cs_runtime_recoveries", "created_at"),
                ("submission_tombstones", "cs_submission_tombstones", "updated_at"),
                ("checkout_intents", "cs_checkout_intents", "updated_at"),
            )
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name, table, time_column in definitions:
                check_export_deadline()
                if table not in tables:
                    continue
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE {time_column} >= ? ORDER BY {time_column}, rowid",
                    (cutoff,),
                )
                counts[name] = write_lines(
                    archive, f"{name}.jsonl",
                    (
                        json.dumps(_export_row(name, dict(row), ref, clean),
                                   ensure_ascii=False, sort_keys=True)
                        for row in rows
                    ),
                )

            actual_configs = []
            if "cs_runtime_configs" in tables:
                actual_configs = [
                    _export_row("runtime_configs", dict(row), ref, clean)
                    for row in connection.execute("SELECT * FROM cs_runtime_configs WHERE updated_at>=? ORDER BY run_id",
                                                  (cutoff,))
                ]
            write_text(archive, "runtime.json", json.dumps({
                "program_version": __version__, "diagnostic_schema": SCHEMA_VERSION,
                "product_fact_guard_version": GUARD_VERSION,
                "contact_policy_version": CONTACT_POLICY_VERSION,
                "recontact_policy_version": RECONTACT_POLICY_VERSION,
                "configuration_source": "recorded_run_settings" if actual_configs else "unavailable_for_legacy_runs",
                "runs": actual_configs,
            }, ensure_ascii=False, indent=2))
            recovery_lines = []
            journal_root = database_path.parent / "send-recovery"
            if journal_root.is_dir():
                for path in journal_root.glob("*.json"):
                    check_export_deadline()
                    try:
                        if path.stat().st_size > 1_000_000:
                            continue
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if str(payload.get("updated_at", "")) < cutoff:
                            continue
                        recovery_lines.append(json.dumps(_export_row("recovery", payload, ref, clean),
                                                         ensure_ascii=False))
                    except (OSError, ValueError, TypeError):
                        recovery_lines.append(json.dumps({
                            "journal_ref": ref("journal", path.name), "parse_error": "发送凭据无法读取",
                        }, ensure_ascii=False))
            counts["send_recovery"] = write_lines(archive, "send_recovery.jsonl", recovery_lines)
            price_recovery_lines = []
            price_root = database_path.parent / "price-recovery"
            if price_root.is_dir():
                for path in price_root.glob("*.json"):
                    check_export_deadline()
                    try:
                        if path.stat().st_size > 1_000_000:
                            raise ValueError("oversized journal")
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(payload, dict):
                            raise TypeError("invalid journal")
                        if str(payload.get("updated_at", "")) >= cutoff:
                            price_recovery_lines.append(json.dumps(
                                _export_row("price_recovery", payload, ref, clean), ensure_ascii=False))
                    except (OSError, ValueError, TypeError):
                        price_recovery_lines.append(json.dumps({
                            "journal_ref": ref("journal", path.name), "parse_error": "改价凭据无法读取",
                        }, ensure_ascii=False))
            counts["price_recovery"] = write_lines(archive, "price_recovery.jsonl", price_recovery_lines)

            def executor_stack_lines() -> Any:
                trace_root = database_path.parent / "diagnostics" / "customer_service"
                trace_cutoff = (current - timedelta(days=15)).timestamp()
                candidates: list[tuple[float, Path]] = []
                for path in trace_root.glob("executor-*-stack.log"):
                    check_export_deadline()
                    try:
                        modified = path.stat().st_mtime
                        if path.is_file() and modified >= trace_cutoff:
                            candidates.append((modified, path))
                    except OSError:
                        continue
                for modified, path in sorted(candidates, reverse=True)[:20]:
                    check_export_deadline()
                    try:
                        with path.open("rb") as stream:
                            stream.seek(0, os.SEEK_END)
                            length = stream.tell()
                            stream.seek(max(0, length - 2 * 1024 * 1024))
                            tail = stream.read(2 * 1024 * 1024)
                        # Stack traces are diagnostic content, even when the user
                        # explicitly exports raw conversation text.
                        sanitized = "\n".join(safe_error(line) for line in
                                              tail.decode("utf-8", errors="replace").splitlines())
                        yield json.dumps({
                            "stack_ref": ref("stack", path.name),
                            "modified_at": datetime.fromtimestamp(modified, tz=current.tzinfo).isoformat(),
                            "tail_bytes": len(tail), "truncated": length > len(tail), "text": sanitized,
                        }, ensure_ascii=False)
                    except OSError:
                        yield json.dumps({"stack_ref": ref("stack", path.name),
                                          "read_error": "执行器堆栈无法读取"}, ensure_ascii=False)

            counts["executor_stacks"] = write_lines(archive, "executor_stacks.jsonl", executor_stack_lines())
            fence_lines = []
            fence_path = database_path.parent / "browser-executor-fence.json"
            if fence_path.is_file():
                check_export_deadline()
                try:
                    if fence_path.stat().st_size > 1_000_000:
                        raise ValueError("oversized executor fence")
                    fence = json.loads(fence_path.read_text(encoding="utf-8"))
                    if not isinstance(fence, dict):
                        raise TypeError("invalid executor fence")
                    fence_lines.append(json.dumps({
                        "diagnostic_only": True, "recovery_authorization": False,
                        "evidence": _safe_runtime_values(_diagnostic_refs(fence, ref)),
                    }, ensure_ascii=False))
                except (OSError, ValueError, TypeError):
                    fence_lines.append(json.dumps({"diagnostic_only": True,
                        "recovery_authorization": False, "read_error": "执行器隔离凭据无法读取"},
                        ensure_ascii=False))
            counts["executor_fence"] = write_lines(archive, "browser_executor_fence.jsonl", fence_lines)

            def fault_log_lines() -> Any:
                for suffix in (".3", ".2", ".1", ""):
                    path = database_path.parent / ("xianyu_assistant.log" + suffix)
                    if not path.is_file():
                        continue
                    try:
                        with path.open(encoding="utf-8", errors="replace") as stream:
                            copying = False
                            for line_count, line in enumerate(stream):
                                if line_count % 128 == 0:
                                    check_export_deadline()
                                if len(line) > 20 and line[4:5] == "-" and line[10:11] == " ":
                                    timestamp = line[:19].replace(" ", "T")
                                    copying = timestamp >= cutoff[:19] and any(mark in line for mark in (
                                        " ERROR ", " WARNING ", "CS_SEND_", "CS_RECEPTION_HALTED", "CS_WORKER_STALL",
                                    ))
                                if copying:
                                    yield safe_error(line.rstrip())
                    except OSError:
                        continue
            write_lines(archive, "fault_log.txt", fault_log_lines())
            failures = connection.execute("""SELECT stop_reason,COUNT(*) AS count
                FROM cs_run_sessions WHERE started_at>=? AND stop_reason IS NOT NULL
                GROUP BY stop_reason ORDER BY count DESC""", (cutoff,)).fetchall()
            write_text(archive, "failure_summary.json", json.dumps([
                {"reason": safe_error(r[0]), "count": r[1]} for r in failures
            ], ensure_ascii=False, indent=2))

            def timeline_lines() -> Any:
                yield "# 客服对话时间线"
                yield "商家来源仅在应用发送并回读确认时标为应用回复。"
                last_conversation: str | None = None
                rows = connection.execute(
                    """SELECT * FROM cs_quality_messages WHERE last_seen_at >= ?
                       ORDER BY conversation_key, COALESCE(platform_time, observed_at), observed_at, rowid""",
                    (cutoff,),
                )
                for row in rows:
                    conv = ref("conv", row["conversation_key"])
                    if conv != last_conversation:
                        yield ""
                        yield f"## {conv}"
                        last_conversation = conv
                    body = clean(row["text"] or f"[{row['message_type']}，无文本]")
                    timestamp = row["platform_time"] or row["observed_at"]
                    label = _SOURCE_LABELS.get(str(row["source"]), str(row["source"]))
                    yield f"- {timestamp} · {label} · {ref('msg', row['message_key'])}: {body}"

            write_lines(archive, "timeline.md", timeline_lines())

            labels = io.StringIO()
            writer = csv.writer(labels)
            writer.writerow(("send_ref", "conversation_ref", "quality", "issue_type", "suggested_reply", "notes"))
            for row in connection.execute(
                "SELECT send_id, conversation_key FROM cs_quality_sends WHERE created_at >= ? ORDER BY created_at",
                (cutoff,),
            ):
                writer.writerow((ref("send", row["send_id"]), ref("conv", row["conversation_key"]), "", "", "", ""))
            write_text(archive, "review_template.csv", labels.getvalue())

            write_text(
                archive, "README.txt",
                "客服真实对话质检包\n"
                "messages.jsonl：按消息去重的对话；direction=incoming 为顾客，outgoing 为商家，platform 为平台，unknown 为角色待确认。\n"
                "source=application_auto 为应用自动回复；application_reviewed 为应用内人工审核回复；\n"
                "merchant_external 为非应用发送，不能据此断定是人工；merchant_source_uncertain 为来源待确认。\n"
                "sends.jsonl：草稿、最终发送文本和回读状态；decisions.jsonl：意图与规则决策。\n"
                "reply_jobs、handoffs、price_changes、send_outbox：现有接待任务及操作审计。\n"
                "events.jsonl：运行和采集空窗；timeline.md：按会话阅读；review_template.csv：质检标注模板。\n"
                "send_attempts.jsonl：发送阶段与异常堆栈；send_checks.jsonl：只读核对次数、阶段与异常；\n"
                "send_recovery.jsonl：提交恢复凭据；\n"
                "price_recovery.jsonl：改价恢复凭据；submission_tombstones.jsonl：持久提交锁和去重事实；\n"
                "browser_operations.jsonl：实际操作、截止时间及结果；runtime_recoveries.jsonl：恢复阶段；\n"
                "runtime_configs.jsonl：每次运行实际使用的参数；旧运行无参数时明确标为不可用。\n"
                "contact_activity、contact_seen_messages、recontact_windows：顾客询问计时、消息去重和15分钟再次接待资格；标识已脱敏。\n"
                "executor_stacks.jsonl：最近15天最多20份执行器线程堆栈，每份仅保留末尾2MiB并脱敏；\n"
                "browser_executor_fence.jsonl：隔离凭据的脱敏诊断副本，不能作为恢复锁或提交许可使用。\n"
                "fault_log.txt：故障日志；failure_summary.json：停止原因汇总；runtime.json：版本与运行参数。\n"
                "本包只覆盖程序实际观察到的页面消息；离线和漏扫时段不能还原。\n",
            )
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "program_version": __version__,
                "exported_at": current.isoformat(timespec="seconds"),
                "range_days": days,
                "text_mode": "raw" if include_raw_text else "redacted",
                "counts": counts,
                "sha256": hashes,
            }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        temp_path.replace(output_path)
        temp_path = None
        return counts
    finally:
        if connection is not None:
            connection.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)


def _export_row(
    name: str,
    row: dict[str, Any],
    ref: Any,
    clean: Any,
) -> dict[str, Any]:
    for field, kind in (
        ("conversation_key", "conv"), ("message_key", "msg"),
        ("outgoing_message_key", "msg"), ("send_id", "send"),
        ("attribution_send_id", "send"), ("turn_id", "turn"),
        ("turn_fingerprint", "turn"),
        ("job_id", "job"), ("platform_product_id", "product"),
        ("expected_product_id", "product"), ("product_key", "product"),
        ("customer_message_key", "msg"), ("expected_last_message_key", "msg"),
        ("order_card_message_key", "msg"),
        ("conversation_ref", "conv"), ("related_send_id", "send"),
        ("attempt_id", "attempt"),
        ("check_id", "check"),
        ("contact_key", "contact"), ("order_id", "order"),
        ("reservation_id", "job"), ("event_key", "event"),
        ("action_id", "action"), ("request_id", "request"), ("permit_id", "permit"),
        ("actor_id", "actor"), ("account_key", "account"),
        ("merchant_account_id", "merchant"), ("platform_customer_id", "buyer"),
        ("window_id", "window"), ("source_message_key", "msg"), ("last_customer_message_key", "msg"),
    ):
        if field in row:
            row[field] = ref(kind, row[field])
    if "customer_message_keys_json" in row:
        row["customer_message_keys_json"] = [
            ref("msg", item) for item in json.loads(row["customer_message_keys_json"])
        ]
    if "baseline_keys_json" in row:
        row["baseline_keys_json"] = [ref("msg", key) for key in json.loads(row["baseline_keys_json"] or "[]")]
    for field in ("evidence_json", "evidence"):
        if row.get(field):
            try:
                evidence = json.loads(row[field]) if isinstance(row[field], str) else dict(row[field])
                if not isinstance(evidence, dict):
                    raise TypeError("invalid evidence object")
            except (ValueError, TypeError):
                row[field] = {"parse_error": "提交证据无法解析，保留人工核对。"}
                continue
            row[field] = _safe_runtime_values(clean(_diagnostic_refs(evidence, ref)))
    for field in ("semantic_json", "decision_json", "diagnostic_json", "order_evidence_json", "settings_json", "role_evidence_json"):
        if row.get(field):
            try:
                row[field] = _safe_runtime_values(clean(_diagnostic_refs(json.loads(row[field]), ref)))
            except (ValueError, TypeError):
                row[field] = clean(row[field])
    for field in (
        "text", "draft_text", "final_text", "product_title", "failure_reason",
        "detail", "stop_reason", "reply_text", "customer_text",
        "reason", "rationale", "product_name", "exception_detail", "traceback_text",
    ):
        if field in row and row[field] is not None:
            row[field] = clean(row[field])
    if name == "messages":
        row["role"] = {
            "incoming": "customer", "outgoing": "merchant", "platform": "platform",
        }.get(str(row["direction"]), "unknown")
        row["source_label"] = _SOURCE_LABELS.get(str(row["source"]), str(row["source"]))
    return row


def _diagnostic_refs(value: Any, ref: Any) -> Any:
    """Keep evidence links consistent with messages, including nested order data."""
    if isinstance(value, list):
        return [_diagnostic_refs(item, ref) for item in value]
    if isinstance(value, dict):
        kinds = {"conversation_key": "conv", "message_key": "msg", "job_id": "job",
                 "send_id": "send", "checkout_send_id": "send", "product_key": "product",
                 "contact_key": "contact", "order_id": "order", "reservation_id": "job",
                 "request_id": "request", "action_id": "action", "permit_id": "permit",
                 "actor_id": "actor", "account_key": "account", "merchant_account_id": "merchant",
                 "platform_customer_id": "buyer", "product_id": "product", "platform_product_id": "product",
                 "last_customer_key": "msg", "order_card_message_key": "msg",
                 "outgoing_message_key": "msg", "job_name": "job"}
        kinds.update(clarification_message_key="msg", continuation_turn_id="turn",
                     source_message_key="msg", last_customer_message_key="msg",
                     recontact_turn_id="turn", recontact_window_id="window", window_id="window")
        return {key: ([ref("msg", identifier) for identifier in item]
                      if key in {"baseline_keys", "current_message_keys", "source_message_keys"}
                      and isinstance(item, list) else
                      [ref("target", identifier) for identifier in item]
                      if key == "owned_targets" and isinstance(item, list) else
                      ref(kinds[key], item) if key in kinds and item else _diagnostic_refs(item, ref))
                for key, item in value.items()}
    return value


def _safe_runtime_values(value: Any) -> Any:
    """Runtime diagnostics never expose credentials, including raw-chat exports."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if any(secret in key.lower() for secret in (
            "password", "api_key", "access_token", "authorization", "credential",
        )) else _safe_runtime_values(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_runtime_values(item) for item in value]
    return safe_error(value) if isinstance(value, str) else value

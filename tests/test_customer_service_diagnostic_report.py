"""The support report is read-only and omits customer text and identifiers."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_report_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "customer_service_diagnostic_report.py"
    spec = importlib.util.spec_from_file_location("customer_service_diagnostic_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_summarizes_failure_without_exposing_message_text(tmp_path: Path) -> None:
    database = tmp_path / "xianyu_assistant.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE cs_run_sessions (
                 id INTEGER PRIMARY KEY, status TEXT, mode TEXT, started_at TEXT,
                 stopped_at TEXT, failure_category TEXT, failure_stage TEXT,
                 exception_type TEXT, stop_reason TEXT);
               CREATE TABLE cs_reply_jobs (status TEXT, draft_text TEXT);
               CREATE TABLE cs_handoff_events (status TEXT, reason TEXT);"""
        )
        connection.execute(
            """INSERT INTO cs_run_sessions VALUES
               (1, 'halted', 'automatic', '2026-09-25', '2026-09-25',
                'runtime_exception', 'customer_service_thread', 'TimeoutError', '私密客户信息')"""
        )
        connection.execute("INSERT INTO cs_reply_jobs VALUES ('HANDOFF', '私密客户信息')")
        connection.execute("INSERT INTO cs_handoff_events VALUES ('open', '私密客户信息')")
    (tmp_path / "xianyu_assistant.log").write_text(
        '2026-09-25 10:00:00 INFO CS_TRACE '
        '{"event":"poll_complete","status":"running","duration_ms":31000,'
        '"candidates":10,"observed":2,"pending":1,"skips":{"open_handoff":3}}\n',
        encoding="utf-8",
    )

    report = _load_report_module().generate_report(tmp_path)

    assert report["database"]["recent_runs"][0]["exception_type"] == "TimeoutError"
    assert report["database"]["reply_jobs_by_status"] == {"HANDOFF": 1}
    assert report["trace"]["polls_over_30_seconds"] == 1
    assert report["trace"]["skip_totals"] == {"open_handoff": 3}
    assert "私密客户信息" not in str(report)

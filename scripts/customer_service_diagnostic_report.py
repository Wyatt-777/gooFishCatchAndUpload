"""Summarize local customer-service runs without exporting customer messages.

Usage: python scripts/customer_service_diagnostic_report.py [--data-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path


def _read_run_summary(database: Path) -> dict[str, object]:
    if not database.exists():
        return {"available": False}
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            result: dict[str, object] = {"available": True}
            if "cs_run_sessions" in tables:
                result["recent_runs"] = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT status, mode, started_at, stopped_at,
                                  failure_category, failure_stage, exception_type
                           FROM cs_run_sessions ORDER BY id DESC LIMIT 20"""
                    )
                ]
            if "cs_reply_jobs" in tables:
                result["reply_jobs_by_status"] = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM cs_reply_jobs GROUP BY status"
                    )
                }
            if "cs_handoff_events" in tables:
                result["handoffs_by_status"] = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM cs_handoff_events GROUP BY status"
                    )
                }
        connection.close()
        return result
    except (OSError, sqlite3.Error) as error:
        return {"available": False, "error_type": type(error).__name__}


def _read_trace_summary(data_dir: Path) -> dict[str, object]:
    files = [data_dir / f"xianyu_assistant.log.{index}" for index in range(3, 0, -1)]
    files.append(data_dir / "xianyu_assistant.log")
    phases: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    events: Counter[str] = Counter()
    latest_poll: dict[str, object] | None = None
    longest_poll_ms = 0
    slow_polls = 0
    for path in files:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    marker = "CS_TRACE "
                    if marker not in line:
                        continue
                    try:
                        event = json.loads(line.split(marker, 1)[1])
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = str(event.get("event", "unknown"))
                    events[kind] += 1
                    if kind == "phase_change":
                        phases[str(event.get("to", "unknown"))] += 1
                    elif kind == "poll_complete":
                        duration = int(event.get("duration_ms", 0))
                        longest_poll_ms = max(longest_poll_ms, duration)
                        slow_polls += duration >= 30_000
                        latest_poll = {
                            "time": line[:19],
                            "status": event.get("status"),
                            "duration_ms": duration,
                            "candidates": event.get("candidates"),
                            "observed": event.get("observed"),
                            "pending": event.get("pending"),
                            "oldest_pending_seconds": event.get("oldest_pending_seconds"),
                            "skips": event.get("skips"),
                        }
                        skips.update(event.get("skips") or {})
        except OSError:
            continue
    return {
        "trace_events": dict(events),
        "phase_entries": dict(phases),
        "skip_totals": dict(skips),
        "longest_poll_ms": longest_poll_ms,
        "polls_over_30_seconds": slow_polls,
        "latest_poll": latest_poll,
    }


def generate_report(data_dir: Path) -> dict[str, object]:
    """Read the running app's database and logs; never modify them."""
    return {
        "data_dir": str(data_dir),
        "database": _read_run_summary(data_dir / "xianyu_assistant.db"),
        "trace": _read_trace_summary(data_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "XianyuAssistant",
    )
    args = parser.parse_args()
    print(json.dumps(generate_report(args.data_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Create a consistent, validated business-data snapshot for the installer."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

_FORBIDDEN_COLUMN_FRAGMENTS = ("api_key", "password", "secret", "access_token")


def stage_database(source: Path, destination: Path) -> None:
    """Back up *source* to *destination* and verify it contains no secret columns."""
    if not source.is_file():
        raise FileNotFoundError(f"正式数据库不存在：{source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    with (
        closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True))
        as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)

    with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"安装数据快照完整性检查失败：{integrity!r}")

        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        suspicious_columns: list[str] = []
        for (table_name,) in tables:
            escaped_table_name = table_name.replace('"', '""')
            columns = connection.execute(
                f'PRAGMA table_info("{escaped_table_name}")'
            ).fetchall()
            for column in columns:
                column_name = str(column[1]).lower()
                if any(fragment in column_name for fragment in _FORBIDDEN_COLUMN_FRAGMENTS):
                    suspicious_columns.append(f"{table_name}.{column[1]}")
        if suspicious_columns:
            raise RuntimeError(
                "数据库存在疑似密钥字段，拒绝打包：" + ", ".join(suspicious_columns)
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    stage_database(args.source, args.destination)
    print(f"Staged database: {args.destination} ({args.destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

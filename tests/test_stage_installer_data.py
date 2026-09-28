"""Tests for safe installer data staging."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.stage_installer_data import stage_database


def test_stage_database_copies_business_data(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "output" / "snapshot.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE products (name TEXT NOT NULL)")
        connection.execute("INSERT INTO products VALUES ('6030')")
        connection.execute("CREATE TABLE cs_settings (name TEXT, value TEXT)")
        connection.execute("INSERT INTO cs_settings VALUES ('first_contact_message', '自定义话术')")
        connection.execute("CREATE TABLE cs_customers (customer_key TEXT)")
        connection.execute("INSERT INTO cs_customers VALUES ('私人顾客记录')")
        connection.execute("CREATE TABLE cs_reply_jobs (draft_text TEXT)")
        connection.execute("INSERT INTO cs_reply_jobs VALUES ('私人聊天内容')")

    stage_database(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM products").fetchall() == [("6030",)]
        assert connection.execute("SELECT value FROM cs_settings").fetchall() == [("自定义话术",)]
        assert connection.execute("SELECT COUNT(*) FROM cs_customers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cs_reply_jobs").fetchone()[0] == 0
    assert "私人顾客记录".encode() not in destination.read_bytes()
    assert "私人聊天内容".encode() not in destination.read_bytes()


def test_stage_database_rejects_secret_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE settings (api_key TEXT)")

    with pytest.raises(RuntimeError, match="疑似密钥字段"):
        stage_database(source, tmp_path / "snapshot.db")

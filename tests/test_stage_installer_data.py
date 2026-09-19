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

    stage_database(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM products").fetchall() == [("6030",)]


def test_stage_database_rejects_secret_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE settings (api_key TEXT)")

    with pytest.raises(RuntimeError, match="疑似密钥字段"):
        stage_database(source, tmp_path / "snapshot.db")

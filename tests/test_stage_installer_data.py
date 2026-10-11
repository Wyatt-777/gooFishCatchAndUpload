"""Tests for safe installer data staging."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.stage_installer_data import stage_database


def test_stage_database_copies_all_saved_data_and_portable_images(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "output" / "snapshot.db"
    image_root = tmp_path / "source-images"
    image_root.mkdir()
    image = image_root / "image001.jpg"
    image.write_bytes(b"saved-image")
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE products (name TEXT NOT NULL)")
        connection.execute("INSERT INTO products VALUES ('6030')")
        connection.execute("CREATE TABLE cs_settings (name TEXT, value TEXT)")
        connection.execute("INSERT INTO cs_settings VALUES ('first_contact_message', '自定义话术')")
        connection.execute("CREATE TABLE cs_customers (customer_key TEXT)")
        connection.execute("INSERT INTO cs_customers VALUES ('私人顾客记录')")
        connection.execute("CREATE TABLE cs_reply_jobs (draft_text TEXT)")
        connection.execute("INSERT INTO cs_reply_jobs VALUES ('私人聊天内容')")
        connection.execute("CREATE TABLE cs_quality_messages (content TEXT)")
        connection.execute("INSERT INTO cs_quality_messages VALUES ('质检中的顾客消息')")
        connection.execute("CREATE TABLE cs_quality_sends (content TEXT)")
        connection.execute("INSERT INTO cs_quality_sends VALUES ('质检中的商家消息')")
        connection.execute(
            "CREATE TABLE product_images (id INTEGER, product_id INTEGER, position INTEGER, local_path TEXT)"
        )
        connection.execute(
            "INSERT INTO product_images VALUES (1, 7, 0, ?)", (str(image),)
        )

    stage_database(
        source,
        destination,
        image_destination=destination.parent / "images",
        allowed_image_roots=(image_root,),
    )

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT name FROM products").fetchall() == [("6030",)]
        assert connection.execute("SELECT value FROM cs_settings").fetchall() == [("自定义话术",)]
        assert connection.execute("SELECT COUNT(*) FROM cs_customers").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cs_reply_jobs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cs_quality_messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cs_quality_sends").fetchone()[0] == 1
        assert connection.execute("SELECT local_path FROM product_images").fetchone()[0] == (
            "images/product007/image001.jpg"
        )
    assert (destination.parent / "images/product007/image001.jpg").read_bytes() == b"saved-image"


def test_stage_database_rejects_secret_columns(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE settings (api_key TEXT)")

    with pytest.raises(RuntimeError, match="疑似密钥字段"):
        stage_database(source, tmp_path / "snapshot.db")


def test_stage_database_rejects_key_values(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE cs_settings (name TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO cs_settings VALUES ('accidental', ?)",
            ("sk-" + "a" * 25,),
        )
    with pytest.raises(RuntimeError, match="API Key"):
        stage_database(source, tmp_path / "snapshot.db")

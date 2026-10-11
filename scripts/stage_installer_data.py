"""Create a portable snapshot of saved application data for the installer."""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

_FORBIDDEN_COLUMN_FRAGMENTS = ("api_key", "password", "secret", "access_token")
_KEY_PATTERN = re.compile(rb"(?i)(?:sk-[a-z0-9_-]{20,}|bearer\s+[a-z0-9._-]{20,})")


def stage_database(
    source: Path,
    destination: Path,
    *,
    image_destination: Path | None = None,
    allowed_image_roots: tuple[Path, ...] = (),
) -> None:
    """Back up all active data, optionally copying images, without credentials."""
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

    # Upgrade only the installer copy; existing profiles migrate on app startup.
    from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository

    repository = CustomerServiceRepository(destination)
    repository.initialize()
    repository.backfill_current_battery_chemistry()

    if image_destination is not None:
        _stage_images(destination, image_destination, allowed_image_roots)
    # Rebuild the copy so deleted source rows cannot survive in free pages.
    with sqlite3.connect(destination) as connection:
        connection.execute("VACUUM")

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

    database_bytes = destination.read_bytes()
    if _KEY_PATTERN.search(database_bytes):
        raise RuntimeError("数据库疑似包含 API Key 或访问令牌，拒绝打包。")
    try:
        import keyring

        saved_api_key = keyring.get_password("XianyuAssistant", "deepseek_api_key")
    except Exception:  # noqa: BLE001 - pattern and schema checks still apply
        saved_api_key = None
    if saved_api_key and saved_api_key.encode("utf-8") in database_bytes:
        raise RuntimeError("数据库包含当前 API Key，拒绝打包。")


def _stage_images(
    database: Path, image_destination: Path, allowed_image_roots: tuple[Path, ...]
) -> None:
    if not allowed_image_roots:
        raise ValueError("打包商品图片时必须指定允许的图片目录。")
    if image_destination.resolve().parent != database.parent.resolve() or image_destination.name != "images":
        raise ValueError("图片暂存目录必须是数据库同级的 images 目录。")
    if image_destination.exists():
        shutil.rmtree(image_destination)
    image_destination.mkdir(parents=True)
    roots = tuple(root.resolve() for root in allowed_image_roots)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "product_images" not in tables:
            return
        rows = connection.execute(
            "SELECT id, product_id, position, local_path FROM product_images WHERE local_path IS NOT NULL"
        ).fetchall()
        for image_id, product_id, position, local_path in rows:
            source = Path(local_path).resolve()
            if not any(source.is_relative_to(root) for root in roots):
                raise RuntimeError(f"商品 {product_id} 的图片不在允许的目录内。")
            if not source.is_file():
                connection.execute("UPDATE product_images SET local_path = NULL WHERE id = ?", (image_id,))
                continue
            suffix = source.suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                raise RuntimeError(f"商品 {product_id} 的图片格式不受支持。")
            target = image_destination / f"product{product_id:03d}" / f"image{position + 1:03d}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            connection.execute(
                "UPDATE product_images SET local_path = ? WHERE id = ?",
                (target.relative_to(database.parent).as_posix(), image_id),
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--allowed-image-root", action="append", type=Path, default=[])
    args = parser.parse_args()
    stage_database(
        args.source, args.destination,
        image_destination=args.images,
        allowed_image_roots=tuple(args.allowed_image_root),
    )
    print(f"Staged database: {args.destination} ({args.destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

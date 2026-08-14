"""SQLite persistence for collection tasks, products, and product images."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from xianyu_assistant.debug.artifacts import DebugArtifactWriter
from xianyu_assistant.domain.models import (
    CollectionTask,
    ProductAttribute,
    ProductCandidate,
    ProductRecord,
    TaskStatus,
)
from xianyu_assistant.domain.task_state import validate_transition


class SqliteRepository:
    """Open short-lived SQLite connections so UI and worker threads remain isolated."""

    def __init__(
        self,
        database_path: Path,
        *,
        debug_writer: DebugArtifactWriter | None = None,
    ) -> None:
        self._database_path = database_path
        self._debug_writer = debug_writer

    def initialize(self) -> None:
        """Create the Phase 4 schema when it does not already exist."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    keyword TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, external_id)
                );

                CREATE TABLE IF NOT EXISTS product_images (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    local_path TEXT,
                    position INTEGER NOT NULL,
                    UNIQUE(product_id, position)
                );

                CREATE TABLE IF NOT EXISTS product_attributes (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    UNIQUE(product_id, position)
                );

                CREATE TABLE IF NOT EXISTS product_categories (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    category_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL,
                    UNIQUE(product_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_products_task_id ON products(task_id);
                CREATE INDEX IF NOT EXISTS idx_product_images_product_id ON product_images(product_id);
                CREATE INDEX IF NOT EXISTS idx_product_attributes_product_id ON product_attributes(product_id);
                CREATE INDEX IF NOT EXISTS idx_product_categories_product_id ON product_categories(product_id);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO product_categories (product_id, category_id, name, position)
                SELECT id, '', category, 0 FROM products WHERE TRIM(category) != ''
                """
            )

    def create_task(self, keyword: str) -> CollectionTask:
        """Persist a new waiting task and return its complete record."""
        now = _timestamp()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks (keyword, status, progress, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (keyword, TaskStatus.WAITING.value, 0, now, now),
            )
            task_id = int(cursor.lastrowid)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> CollectionTask:
        """Return one task or raise ``KeyError`` when it no longer exists."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"未找到任务 {task_id}。")
        return _task_from_row(row)

    def list_tasks(self) -> list[CollectionTask]:
        """Return newest tasks first for the left-side task list."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        return [_task_from_row(row) for row in rows]

    def transition_task(
        self,
        task_id: int,
        target: TaskStatus,
        *,
        progress: int | None = None,
        error_message: str | None = None,
    ) -> CollectionTask:
        """Move a task through the validated lifecycle and store an optional error."""
        current = self.get_task(task_id)
        validate_transition(current.status, target)
        next_progress = current.progress if progress is None else progress
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, progress = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (target.value, next_progress, error_message, _timestamp(), task_id),
            )
        return self.get_task(task_id)

    def save_products(self, task_id: int, products: Iterable[ProductCandidate]) -> int:
        """Upsert products and images, returning the number of newly inserted products."""
        inserted_count = 0
        saved_categories: list[tuple[int, ProductCandidate]] = []
        now = _timestamp()
        with self._connection() as connection:
            for product in products:
                existing = connection.execute(
                    "SELECT id FROM products WHERE task_id = ? AND external_id = ?",
                    (task_id, product.external_id),
                ).fetchone()
                if existing is None:
                    inserted_count += 1
                connection.execute(
                    """
                    INSERT INTO products (
                        task_id, external_id, title, price, description, category, url, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, external_id) DO UPDATE SET
                        title = excluded.title,
                        price = excluded.price,
                        description = excluded.description,
                        category = excluded.category,
                        url = excluded.url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task_id,
                        product.external_id,
                        product.title,
                        product.price,
                        product.description,
                        product.category,
                        product.url,
                        now,
                        now,
                    ),
                )
                product_id = int(
                    connection.execute(
                        "SELECT id FROM products WHERE task_id = ? AND external_id = ?",
                        (task_id, product.external_id),
                    ).fetchone()["id"]
                )
                connection.execute("DELETE FROM product_images WHERE product_id = ?", (product_id,))
                connection.executemany(
                    """
                    INSERT INTO product_images (product_id, source_url, position)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (product_id, image_url, position)
                        for position, image_url in enumerate(product.image_urls)
                    ],
                )
                connection.execute(
                    "DELETE FROM product_attributes WHERE product_id = ?", (product_id,)
                )
                connection.executemany(
                    """
                    INSERT INTO product_attributes (product_id, name, value, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (product_id, attribute.name, attribute.value, position)
                        for position, attribute in enumerate(product.attributes)
                    ],
                )
                connection.execute(
                    "DELETE FROM product_categories WHERE product_id = ?", (product_id,)
                )
                connection.executemany(
                    """
                    INSERT INTO product_categories (product_id, category_id, name, position)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (product_id, category_id, name, position)
                        for position, (category_id, name) in enumerate(_category_rows(product))
                    ],
                )
                saved_categories.append((product_id, product))
        if self._debug_writer is not None:
            for product_id, product in saved_categories:
                self._debug_writer.record_category_saved(
                    product_id=product_id,
                    external_id=product.external_id,
                    category_path=product.category_path,
                    category_ids=product.category_ids,
                )
        return inserted_count

    def list_products(self, task_id: int) -> list[ProductRecord]:
        """Return products in collection order, including every source image URL."""
        with self._connection() as connection:
            product_rows = connection.execute(
                "SELECT * FROM products WHERE task_id = ? ORDER BY id DESC", (task_id,)
            ).fetchall()
            return self._build_product_records(connection, product_rows)

    def list_all_products(self) -> list[ProductRecord]:
        """Return every collected product for the workbook export."""
        with self._connection() as connection:
            product_rows = connection.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
            return self._build_product_records(connection, product_rows)

    def update_image_path(self, product_id: int, position: int, local_path: str) -> None:
        """Store the downloaded local image path for one product image position."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE product_images SET local_path = ?
                WHERE product_id = ? AND position = ?
                """,
                (local_path, product_id, position),
            )
            row_count = cursor.rowcount
        if row_count != 1:
            raise KeyError(f"未找到商品 {product_id} 的第 {position + 1} 张图片。")

    @staticmethod
    def _build_product_records(
        connection: sqlite3.Connection,
        product_rows: list[sqlite3.Row],
    ) -> list[ProductRecord]:
        if not product_rows:
            return []
        product_ids = [int(row["id"]) for row in product_rows]
        placeholders = ", ".join("?" for _ in product_ids)
        image_rows = connection.execute(
            f"""
            SELECT product_id, source_url, local_path FROM product_images
            WHERE product_id IN ({placeholders})
            ORDER BY position
            """,
            product_ids,
        ).fetchall()
        images: defaultdict[int, list[tuple[str, str | None]]] = defaultdict(list)
        for row in image_rows:
            images[int(row["product_id"])].append((str(row["source_url"]), row["local_path"]))
        attribute_rows = connection.execute(
            f"""
            SELECT product_id, name, value FROM product_attributes
            WHERE product_id IN ({placeholders})
            ORDER BY position
            """,
            product_ids,
        ).fetchall()
        attributes: defaultdict[int, list[ProductAttribute]] = defaultdict(list)
        for row in attribute_rows:
            attributes[int(row["product_id"])].append(
                ProductAttribute(name=str(row["name"]), value=str(row["value"]))
            )
        category_rows = connection.execute(
            f"""
            SELECT product_id, category_id, name FROM product_categories
            WHERE product_id IN ({placeholders})
            ORDER BY position
            """,
            product_ids,
        ).fetchall()
        categories: defaultdict[int, list[tuple[str, str]]] = defaultdict(list)
        for row in category_rows:
            categories[int(row["product_id"])].append((str(row["category_id"]), str(row["name"])))
        return [
            ProductRecord(
                id=int(row["id"]),
                task_id=int(row["task_id"]),
                external_id=str(row["external_id"]),
                title=str(row["title"]),
                price=str(row["price"]),
                description=str(row["description"]),
                category=str(row["category"]),
                url=str(row["url"]),
                image_urls=tuple(image[0] for image in images[int(row["id"])]),
                image_paths=tuple(image[1] for image in images[int(row["id"])]),
                category_path=tuple(name for _, name in categories[int(row["id"])] if name),
                category_ids=tuple(
                    category_id for category_id, _ in categories[int(row["id"])] if category_id
                ),
                attributes=tuple(attributes[int(row["id"])]),
            )
            for row in product_rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back each operation, then release the SQLite file handle."""
        connection = self._connect()
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def _category_rows(product: ProductCandidate) -> tuple[tuple[str, str], ...]:
    """Keep category IDs and names aligned by hierarchy position when either exists."""
    names = product.category_path or ((product.category,) if product.category else ())
    row_count = max(len(product.category_ids), len(names))
    return tuple(
        (
            product.category_ids[position] if position < len(product.category_ids) else "",
            names[position] if position < len(names) else "",
        )
        for position in range(row_count)
    )


def _task_from_row(row: sqlite3.Row) -> CollectionTask:
    return CollectionTask(
        id=int(row["id"]),
        keyword=str(row["keyword"]),
        status=TaskStatus(str(row["status"])),
        progress=int(row["progress"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        error_message=row["error_message"],
    )


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

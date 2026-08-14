"""Small, local JSON artifacts for investigating source-data mismatches."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DebugArtifactWriter:
    """Write replaceable per-product diagnostics without affecting collection results."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or Path.cwd() / "debug"

    def record_images(
        self,
        *,
        external_id: str,
        product_url: str,
        candidates: Iterable[Mapping[str, str]],
        selected_urls: Iterable[str],
    ) -> None:
        self._write(
            self._root / "images" / f"{_safe_name(external_id)}.json",
            {
                "product_url": product_url,
                "candidate_images": [dict(candidate) for candidate in candidates],
                "selected_image_urls": list(selected_urls),
            },
        )

    def record_category_collection(
        self,
        *,
        external_id: str,
        product_url: str,
        raw_detail_category: Mapping[str, Any],
        category_path: Iterable[str],
        category_ids: Iterable[str],
    ) -> None:
        self._write(
            self._root / "category" / f"{_safe_name(external_id)}.json",
            {
                "product_url": product_url,
                "raw_detail_category": dict(raw_detail_category),
                "parsed_category_path": list(category_path),
                "parsed_category_ids": list(category_ids),
            },
        )

    def record_category_saved(
        self,
        *,
        product_id: int,
        external_id: str,
        category_path: Iterable[str],
        category_ids: Iterable[str],
    ) -> None:
        self._write(
            self._root / "category" / f"{_safe_name(external_id)}.json",
            {
                "product_id": product_id,
                "saved_category_path": list(category_path),
                "saved_category_ids": list(category_ids),
            },
        )

    def record_category_publish(
        self,
        *,
        product_id: int,
        category_input: str,
        category_path: Iterable[str],
        category_ids: Iterable[str],
    ) -> None:
        self._write(
            self._root / "category" / f"product-{product_id}.json",
            {
                "product_id": product_id,
                "publish_category_input": category_input,
                "publish_category_path": list(category_path),
                "publish_category_ids": list(category_ids),
            },
        )

    def record_publish_attributes(
        self,
        *,
        product_id: int,
        results: Iterable[Mapping[str, str]],
    ) -> None:
        self._write(
            self._root / "attributes" / f"product-{product_id}.json",
            {
                "attribute_results": [dict(result) for result in results],
            },
        )

    def _write(self, path: Path, values: Mapping[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            current: dict[str, Any] = {}
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            current.update(values)
            path.write_text(
                json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as error:
            logger.warning("Unable to write debug artifact %s: %s", path, error)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown-product"

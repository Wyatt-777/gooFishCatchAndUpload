"""Import installer data into the account that actually launches the app."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def seed_installed_data(database_path: Path, seed_directory: Path) -> bool:
    """Seed a fresh profile once; an existing user's database is never replaced."""
    source_database = seed_directory / "xianyu_assistant.db"
    if database_path.exists() or not source_database.is_file():
        return False

    database_path.parent.mkdir(parents=True, exist_ok=True)
    source_images = seed_directory / "images"
    if source_images.is_dir():
        for source in source_images.rglob("*"):
            if not source.is_file():
                continue
            target = database_path.parent / "images" / source.relative_to(source_images)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)

    temporary = database_path.with_suffix(".seed-part")
    try:
        shutil.copy2(source_database, temporary)
        os.replace(temporary, database_path)
    finally:
        temporary.unlink(missing_ok=True)
    return True

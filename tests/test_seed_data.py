"""Installed data must seed the launching account without overwriting local work."""

from pathlib import Path

from xianyu_assistant.persistence.seed_data import seed_installed_data


def test_seed_installed_data_copies_database_and_images_once(tmp_path: Path) -> None:
    seed = tmp_path / "installed" / "seed-data"
    image = seed / "images" / "product001" / "image001.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    (seed / "xianyu_assistant.db").write_bytes(b"saved-database")
    database = tmp_path / "user" / "xianyu_assistant.db"

    assert seed_installed_data(database, seed) is True
    assert database.read_bytes() == b"saved-database"
    assert (database.parent / "images" / "product001" / "image001.jpg").read_bytes() == b"image"

    database.write_bytes(b"user-updated-database")
    assert seed_installed_data(database, seed) is False
    assert database.read_bytes() == b"user-updated-database"

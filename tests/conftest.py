"""Qt test fixtures that work without a visible desktop session."""

import os

import pytest
from PyQt6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Provide the single QApplication instance required by all widget tests."""
    return QApplication.instance() or QApplication([])

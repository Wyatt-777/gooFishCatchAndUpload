"""Production clock implementation for dependency-injected customer-service code."""

from __future__ import annotations

from datetime import datetime
from time import monotonic


class SystemClock:
    """Use local timezone wall time plus a monotonic elapsed-time source."""

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return monotonic()

"""Замер длительности шагов."""

from __future__ import annotations

import time

__all__ = ["Elapsed"]


class Elapsed:
    """Длительность шага: заводится перед операцией, читается после неё."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)


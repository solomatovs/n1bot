"""Lazy cache discovered `ToolSchema`'s — заполняется при первом ChatSession build'е."""

from __future__ import annotations

import threading

from boba.tools.domain.tool import ToolSchema

__all__ = ["AvailableToolsCache"]


class AvailableToolsCache:
    """Thread-safe set-once кеш списка доступных в проце tool'ов.

    Заполняется при первом успешном build'е ChatSession (см.
    `ChatSession._wrap_catalog`). Discovery плагинов — статика времени
    старта, повторное заполнение игнорируется. Читается UI-слоем для
    отрисовки шестерёнки.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._schemas: tuple[ToolSchema, ...] | None = None

    def set_once(self, schemas: list[ToolSchema]) -> None:
        with self._lock:
            if self._schemas is None:
                self._schemas = tuple(schemas)

    def schemas(self) -> tuple[ToolSchema, ...] | None:
        with self._lock:
            return self._schemas

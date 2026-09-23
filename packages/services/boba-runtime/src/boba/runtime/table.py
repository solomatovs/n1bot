"""Таблицы чата на общей базе postgres: отказ базы уходит наружу ошибкой слоя данных.

Ошибки:
DataUnavailableError — пул, соединение или запрос отказали.
"""

from __future__ import annotations

from typing import ClassVar

from boba.chat.threads import DataUnavailableError
from boba.db.postgres import PostgresTable

__all__ = ["PgTable"]


class PgTable(PostgresTable):
    """Таблица схемы чата: базовый класс UsersTable, ThreadsTable, ElementsTable
    и FeedbacksTable; отказ базы уходит DataUnavailableError с именем операции."""

    LABEL: ClassVar[str] = "chat"

    def _failure(self, action: str, exc: Exception) -> Exception:
        return DataUnavailableError(action, self._detail(action, exc))

"""Реализация порта истории агента над Postgres.

Записи сессии лежат в таблице history_records по порядку seq; захват сессии —
advisory lock соединения.
"""

from boba.history.postgres.store import PostgresHistoryStore

__all__ = ["PostgresHistoryStore"]

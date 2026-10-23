"""Реализация порта истории агента над Postgres.

Сессии лежат в таблице session, их записи — в таблице history по порядку seq:
каркас записи колонками, остальное документом jsonb; захват сессии —
advisory lock соединения.
"""

from boba.history.postgres.store import PostgresHistoryStore

__all__ = ["PostgresHistoryStore"]

"""Реализация порта истории агента над Postgres.

Сессии лежат в таблице session, их записи — в таблице session_records по порядку seq:
каркас записи колонками, остальное документом jsonb; захват сессии —
advisory lock соединения.
"""

from boba.history.postgres.store import PostgresHistoryStore, SessionLocks

__all__ = ["PostgresHistoryStore", "SessionLocks"]

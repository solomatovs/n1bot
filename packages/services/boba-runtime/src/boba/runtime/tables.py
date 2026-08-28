"""Таблицы и колонки схемы чата: одни имена у data layer chainlit и запросов studio."""

from __future__ import annotations

from enum import StrEnum

from psycopg import sql

__all__ = ["ChatTable", "ThreadsColumn", "UsersColumn"]


class ChatTable(StrEnum):
    """Таблицы схемы чата; DDL держит data layer chainlit."""

    USERS = "users"
    THREADS = "threads"
    ELEMENTS = "elements"
    FEEDBACKS = "feedbacks"

    def under(self, schema: str) -> sql.Identifier:
        return sql.Identifier(schema, self.value)


class UsersColumn(StrEnum):
    """Колонки users; имена совпадают с полями строки User data layer."""

    ID = "id"
    UUID = "user_uuid"
    IDENTIFIER = "identifier"
    CREATED_AT = "created_at"
    META = "meta"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class ThreadsColumn(StrEnum):
    """Колонки threads, нужные вне data layer."""

    ID = "id"
    USER_ID = "user_id"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)

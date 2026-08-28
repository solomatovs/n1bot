"""Имена таблиц и колонок схемы чата, общие для data layer chainlit и запросов studio,
чтобы SQL обоих приложений не расходился.
"""

from __future__ import annotations

from enum import StrEnum

from psycopg import sql

__all__ = [
    "ChatTable",
    "LiveChannel",
    "LiveCommandsColumn",
    "LiveEventsColumn",
    "LiveInstancesColumn",
    "LiveLocksColumn",
    "ThreadsColumn",
    "UsersColumn",
]


class ChatTable(StrEnum):
    """Таблицы схемы чата; DDL держит data layer chainlit, шина создаёт свои таблицы
    сама.
    """

    USERS = "users"
    THREADS = "threads"
    ELEMENTS = "elements"
    FEEDBACKS = "feedbacks"
    LIVE_INSTANCES = "live_instances"
    LIVE_LOCKS = "live_locks"
    LIVE_EVENTS = "live_events"
    LIVE_COMMANDS = "live_commands"

    def under(self, schema: str) -> sql.Identifier:
        return sql.Identifier(schema, self.value)


class UsersColumn(StrEnum):
    """Колонки users; имена совпадают с полями строки User в data layer."""

    ID = "id"
    UUID = "user_uuid"
    IDENTIFIER = "identifier"
    CREATED_AT = "created_at"
    META = "meta"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class ThreadsColumn(StrEnum):
    """Колонки threads, к которым обращаются вне data layer."""

    ID = "id"
    USER_ID = "user_id"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class LiveChannel(StrEnum):
    """Каналы LISTEN/NOTIFY; один канал на всё приложение, содержимое различает
    указатель.
    """

    LIVE = "boba_live"


class LiveInstancesColumn(StrEnum):
    """Колонки live_instances: процессы, зарегистрированные в базе, и их последнее
    подтверждение жизни.
    """

    INSTANCE_ID = "instance_id"
    APP = "app"
    HOST = "host"
    STARTED_AT = "started_at"
    HEARTBEAT_AT = "heartbeat_at"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class LiveEventsColumn(StrEnum):
    """Колонки live_events: сообщения шины, пронумерованные внутри области."""

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    SEQ = "seq"
    KIND = "kind"
    ORIGIN = "origin"
    BODY = "body"
    AT = "at"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class LiveCommandsColumn(StrEnum):
    """Колонки live_commands: команды областям и кто из инстансов их исполнил."""

    ID = "id"
    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    ACTION = "action"
    BODY = "body"
    BY_INSTANCE = "by_instance"
    AT = "at"
    TAKEN_BY = "taken_by"
    TAKEN_AT = "taken_at"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class LiveLocksColumn(StrEnum):
    """Колонки live_locks: кто и зачем держит область и когда подтверждал жизнь."""

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    MODE = "mode"
    HOLDER = "holder"
    TOKEN = "token"  # noqa: S105
    PURPOSE = "purpose"
    USER_ID = "user_id"
    ACQUIRED_AT = "acquired_at"
    HEARTBEAT_AT = "heartbeat_at"
    TTL_SEC = "ttl_sec"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)

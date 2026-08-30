"""Имена таблиц и колонок схемы чата, общие для data layer chainlit и запросов
studio: SQL обоих приложений собирается из одних значений.
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
    "LivePayloadsColumn",
    "ThreadsColumn",
    "UsersColumn",
]


class ChatTable(StrEnum):
    """Таблицы схемы чата: DDL таблиц чата держит data layer chainlit, live-таблицы
    создаёт шина.
    """

    USERS = "users"
    THREADS = "threads"
    ELEMENTS = "elements"
    FEEDBACKS = "feedbacks"
    LIVE_INSTANCES = "live_instances"
    LIVE_LOCKS = "live_locks"
    LIVE_EVENTS = "live_events"
    LIVE_COMMANDS = "live_commands"
    LIVE_PAYLOADS = "live_payloads"

    def under(self, schema: str) -> sql.Identifier:
        return sql.Identifier(schema, self.value)


class UsersColumn(StrEnum):
    """Колонки users; имена совпадают с полями строки User в data layer."""

    ID = "id"
    IDENTIFIER = "identifier"
    CREATED_AT = "created_at"
    META = "meta"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class ThreadsColumn(StrEnum):
    """Колонки threads."""

    ID = "id"
    CREATED_AT = "created_at"
    NAME = "name"
    USER_ID = "user_id"
    TAGS = "tags"
    META = "meta"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class ElementsColumn(StrEnum):
    """Колонки elements."""

    ID = "id"
    NAME = "name"
    TYPE = "type"
    DISPLAY = "display"
    THREAD_ID = "thread_id"
    FOR_ID = "for_id"
    CHAINLIT_KEY = "chainlit_key"
    SIZE = "size"
    LANGUAGE = "language"
    PAGE = "page"
    PROPS = "props"
    MIME = "mime"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class FeedbacksColumn(StrEnum):
    """Колонки feedbacks."""

    ID = "id"
    FOR_ID = "for_id"
    VALUE = "value"
    THREAD_ID = "thread_id"
    COMMENT = "comment"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)


class LiveChannel(StrEnum):
    """Каналы LISTEN/NOTIFY; канал один на всё приложение, вид уведомления различает
    указатель в теле.
    """

    LIVE = "boba_live"


class LiveInstancesColumn(StrEnum):
    """Колонки live_instances: зарегистрированные процессы и время их последнего
    подтверждения жизни.
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
    """Колонки live_locks: кто, зачем и в каком режиме держит область и когда
    подтверждал жизнь.
    """

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


class LivePayloadsColumn(StrEnum):
    """Колонки live_payloads: тела сообщений, привязанные к области."""

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    ID = "id"
    BODY = "body"
    AT = "at"

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.value)

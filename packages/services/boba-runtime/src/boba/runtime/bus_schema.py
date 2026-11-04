"""Имена таблиц, колонок и каналов PostgreSQL-хранилища сообщений.

Ошибки: своих не выпускает.
"""

from enum import StrEnum


class LiveTable(StrEnum):
    """Таблицы PostgreSQL-транспорта сообщений и блокировок."""

    INSTANCES = "live_instances"
    EVENTS = "live_events"
    COMMANDS = "live_commands"
    LOCKS = "live_locks"
    PAYLOADS = "live_payloads"


class LiveSequence(StrEnum):
    """Последовательности шины: номер события общий на таблицу, чтобы seq области
    рос и после уборки её строк.
    """

    EVENTS = "live_events_seq"


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


class LiveEventsColumn(StrEnum):
    """Колонки live_events: сообщения шины, пронумерованные внутри области."""

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    SEQ = "seq"
    KIND = "kind"
    ORIGIN = "origin"
    BODY = "body"
    AT = "at"


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


class LivePayloadsColumn(StrEnum):
    """Колонки live_payloads: тела сообщений, привязанные к области."""

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    ID = "id"
    BODY = "body"
    AT = "at"


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

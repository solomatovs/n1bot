"""SQL-пакеты истории в порядке внешних ключей: пользователи, затем сессии."""

from enum import StrEnum

import boba.identity_postgres as identity_schema


class HistorySchema(StrEnum):
    """Владельцы таблиц для явного создания хранилища истории."""

    IDENTITY = identity_schema.__name__
    HISTORY = "boba.history.postgres"

"""Вызов postgres-payload'а: соединение и запрос идут внутри песочницы.

Ошибки: LauncherError — исполнитель нарушил контракт; PayloadFailureError —
payload объявил ожидаемый отказ (СУБД недоступна, запрос отклонён).
"""

from __future__ import annotations

from typing import Any, ClassVar

from boba.db.postgres import PostgresConfig
from boba.tool.pg.protocol import (
    PgCopyRequest,
    PgCopyTrailer,
    PgQueryRequest,
)
from boba.toolkit.launcher import ChunkSink
from boba.toolkit.sql import SqlPayloadCaller, SqlQueryRequest

__all__ = ["PgCaller", "PgParams"]

PgParams = tuple[Any, ...]
"""Позиционные параметры psycopg под плейсхолдеры %s."""


class PgCaller(SqlPayloadCaller[PostgresConfig, PgParams]):
    """Запрос строк общим вызовом плюс своя выгрузка COPY."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.pg.payload")

    REQUEST: ClassVar[type[SqlQueryRequest[Any, Any]]] = PgQueryRequest

    def copy(
        self,
        *,
        connection: PostgresConfig,
        sql: str,
        max_bytes: int,
        sink: ChunkSink,
    ) -> PgCopyTrailer:
        request = PgCopyRequest(
            op=PgCopyRequest.OP,
            connection=connection,
            sql=sql,
            max_bytes=max_bytes,
        )

        return self._caller.call_stream(type(self).ENTRY, request, sink, PgCopyTrailer)

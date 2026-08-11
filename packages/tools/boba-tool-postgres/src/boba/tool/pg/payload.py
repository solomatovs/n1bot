"""Операции postgres: соединение и запрос идут из песочницы.

Учётные данные приезжают каналом tool_args — не видны ни в argv, ни в /proc,
ни в логах; строки и выгрузка уходят каналом данных, итог — квитанцией.

Ошибки: PostgresError — до базы не достучаться, и sql_failed из PayloadError —
СУБД отклонила запрос; обе объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.tool.pg.protocol import (
    PgCopyRequest,
    PgCopyTrailer,
    PgQueryRequest,
    PgStage,
)
from boba.toolkit.channels import StreamCodec
from boba.toolkit.payload import (
    PayloadChannels,
    PayloadEntry,
    PayloadError,
    PayloadOutputClosedError,
    PayloadStream,
)
from boba.toolkit.sql import SqlQueryTrailer, SqlRows


class PostgresOps:
    """Исполнение SQL; операцию выбирает реестр запросов по полю op."""

    ENCODING: ClassVar[str] = "utf-8"
    """psycopg принимает динамический оператор только байтами (Query)."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        PostgresError: "database_unavailable",
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        PgStage.QUERY: PgQueryRequest,
        PgStage.COPY: PgCopyRequest,
    }

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        if isinstance(request, PgQueryRequest):
            return await cls.query(request, channels)

        if isinstance(request, PgCopyRequest):
            return await cls.copy(request, channels)

        msg = f"unsupported postgres request: {type(request).__name__}"
        raise TypeError(msg)

    @classmethod
    async def query(
        cls, request: PgQueryRequest, channels: PayloadChannels
    ) -> SqlQueryTrailer:
        """Запрос с лимитом: NDJSON-строки в канал данных, либо счётчик без выборки."""
        params = request.params
        if not params:
            params = None

        statement = request.sql.encode(cls.ENCODING)

        stream = channels.payload()

        conn = await PayloadPostgres.connect(request.connection)
        async with conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(statement, params)

                if cur.description is None:
                    return cls._affected(cur)

                truncated = await cls._write_rows(cur, stream, request.row_limit)
            except PayloadOutputClosedError:
                # потребитель закрыл чтение: строки больше не нужны, квитанция едет
                truncated = True
            except psycopg.Error as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e

            return SqlQueryTrailer(
                truncated=truncated,
                returns_rows=True,
                rowcount=None,
                status=cur.statusmessage,
            )

    @classmethod
    async def _write_rows(
        cls,
        cur: psycopg.AsyncCursor[DictRow],
        stream: PayloadStream,
        limit: int,
    ) -> bool:
        """Строка на запись; строка сверх лимита ловит усечение и рвёт поток."""
        written = 0
        async for row in cur:
            if written >= limit:
                return True

            stream.write(StreamCodec.encode_row(SqlRows.of_mapping(row)))
            written += 1

        return False

    @classmethod
    def _affected(cls, cur: psycopg.AsyncCursor[DictRow]) -> SqlQueryTrailer:
        """Итог запроса без выборки; rowcount -1 у psycopg значит «счётчика нет»."""
        rowcount: int | None = cur.rowcount
        if cur.rowcount < 0:
            rowcount = None

        return SqlQueryTrailer(
            truncated=False,
            returns_rows=False,
            rowcount=rowcount,
            status=cur.statusmessage,
        )

    @classmethod
    async def copy(
        cls, request: PgCopyRequest, channels: PayloadChannels
    ) -> PgCopyTrailer:
        """COPY ... TO STDOUT: блоки уходят в канал данных как есть, байтами."""
        statement = request.copy_format.statement(request.sql).encode(cls.ENCODING)

        stream = channels.payload()

        conn = await PayloadPostgres.connect(request.connection)
        async with conn, conn.cursor() as cur:
            try:
                await cls._write_copy(cur, stream, statement)
            except PayloadOutputClosedError:
                # потребитель закрыл чтение: выгрузка прекращена, квитанция едет
                return cls._copied(cur)
            except psycopg.Error as e:
                msg = f"copy failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e

            return cls._copied(cur)

    @classmethod
    async def _write_copy(
        cls,
        cur: psycopg.AsyncCursor[Any],
        stream: PayloadStream,
        statement: bytes,
    ) -> None:
        async with cur.copy(statement) as copy_out:
            async for block in copy_out:
                stream.write(bytes(block))

    @classmethod
    def _copied(cls, cur: psycopg.AsyncCursor[Any]) -> PgCopyTrailer:
        """Число выгруженных строк; psycopg ставит его после закрытия COPY."""
        # -1 у psycopg значит «счётчика нет»: для квитанции это ноль строк
        rows = max(cur.rowcount, 0)

        return PgCopyTrailer(rows=rows)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(PostgresOps))

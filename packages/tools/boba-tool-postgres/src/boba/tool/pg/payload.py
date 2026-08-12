"""Операции postgres: соединение и запрос идут из песочницы.

Учётные данные приезжают каналом tool_args — не видны ни в argv, ни в /proc,
ни в логах; строки и выгрузка уходят каналом данных, вход заливки читается из
tool_stdin, итог — квитанцией.

Ошибки: PostgresError — до базы не достучаться, и PayloadError видов
FailureKind (sql_failed — СУБД отклонила запрос или транзакцию, no_input —
заливке не подан вход); все объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, BinaryIO, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.tool.pg.protocol import (
    PgCopyDirection,
    PgCopyRequest,
    PgCopyTrailer,
    PgQueryRequest,
    PgStage,
)
from boba.toolkit.channels import Channel, StreamCodec
from boba.toolkit.payload import (
    FailureKind,
    PayloadChannels,
    PayloadEntry,
    PayloadError,
    PayloadOutputClosedError,
    PayloadStream,
)
from boba.toolkit.sql import SqlQueryTrailer, SqlRows


class PgErrorText:
    """Текст отказа СУБД для пользователя: класс ошибки и её код, без эха ввода.

    Сообщение psycopg о сбойной строке COPY несёт саму строку — содержимое
    чужой таблицы или файла, — поэтому в конверт едут только тип и sqlstate.
    """

    UNKNOWN: ClassVar[str] = "unknown"

    @classmethod
    def of(cls, error: psycopg.Error) -> str:
        name = type(error).__name__

        sqlstate = error.sqlstate
        if sqlstate is None:
            sqlstate = cls.UNKNOWN

        return f"{name} (SQLSTATE {sqlstate})"


class PostgresOps:
    """Исполнение SQL; операцию выбирает реестр запросов по полю op."""

    ENCODING: ClassVar[str] = "utf-8"
    """psycopg принимает динамический оператор только байтами (Query)."""

    BLOCK_BYTES: ClassVar[int] = 65536
    """Размер блока чтения входного канала при заливке COPY FROM STDIN."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        PostgresError: FailureKind.UNAVAILABLE,
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
        """Запрос с лимитом: NDJSON-строки в канал данных, либо счётчик без выборки.

        Транзакция закрывается внутри охраны: сбой на коммите — тот же
        ожидаемый отказ, что и сбой самого запроса.
        """
        stream = channels.payload()

        conn = await PayloadPostgres.connect(request.connection)

        try:
            async with conn, conn.cursor(row_factory=dict_row) as cur:
                trailer = await cls._executed(cur, request, stream)
        except psycopg.Error as e:
            msg = f"query failed: {type(e).__name__}: {e}"
            raise PayloadError(FailureKind.SQL_FAILED, msg) from e

        return trailer

    @classmethod
    async def _executed(
        cls,
        cur: psycopg.AsyncCursor[DictRow],
        request: PgQueryRequest,
        stream: PayloadStream,
    ) -> SqlQueryTrailer:
        """Исполнение и выдача строк; уход потребителя — усечение, а не отказ."""
        params = request.params
        if not params:
            params = None

        statement = request.sql.encode(cls.ENCODING)

        await cur.execute(statement, params)

        if cur.description is None:
            return cls._affected(cur)

        try:
            truncated = await cls._write_rows(cur, stream, request.row_limit)
        except PayloadOutputClosedError:
            # потребитель закрыл чтение: строки больше не нужны, квитанция едет
            truncated = True

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
        """COPY в обе стороны; направление объявлено в запросе узла."""
        if request.direction is PgCopyDirection.FROM_STDIN:
            return await cls._copy_in(request, channels)

        return await cls._copy_out(request, channels)

    @classmethod
    async def _copy_out(
        cls, request: PgCopyRequest, channels: PayloadChannels
    ) -> PgCopyTrailer:
        """COPY ... TO STDOUT: блоки уходят в канал данных как есть, байтами."""
        statement = request.sql.encode(cls.ENCODING)

        stream = channels.payload()

        conn = await PayloadPostgres.connect(request.connection)

        try:
            async with conn, conn.cursor() as cur:
                trailer = await cls._dumped(cur, stream, statement)
        except psycopg.Error as e:
            msg = f"copy failed: {type(e).__name__}: {e}"
            raise PayloadError(FailureKind.SQL_FAILED, msg) from e

        return trailer

    @classmethod
    async def _dumped(
        cls,
        cur: psycopg.AsyncCursor[Any],
        stream: PayloadStream,
        statement: bytes,
    ) -> PgCopyTrailer:
        """Выгрузка до конца либо до ухода потребителя; квитанция едет и там, и там."""
        try:
            await cls._write_copy(cur, stream, statement)
        except PayloadOutputClosedError:
            # потребитель закрыл чтение: выгрузка прекращена, квитанция едет
            return cls._copied(cur, PgCopyDirection.TO_STDOUT)

        return cls._copied(cur, PgCopyDirection.TO_STDOUT)

    @classmethod
    async def _copy_in(
        cls, request: PgCopyRequest, channels: PayloadChannels
    ) -> PgCopyTrailer:
        """COPY ... FROM STDIN: сырые байты входного канала уходят в оператор.

        Заливка идёт явной транзакцией независимо от autocommit профиля: иначе
        сервер фиксирует строки по ходу, и обрыв источника оставляет таблицу
        наполовину заполненной. Коммит — только после полного оператора; сбой
        и смерть стадии откатывают его.
        """
        if not channels.has(Channel.TOOL_STDIN):
            msg = (
                "COPY FROM STDIN has no input: the node has neither an "
                "incoming edge nor a stdin literal"
            )
            raise PayloadError(FailureKind.NO_INPUT, msg)

        source = channels.stdin()

        statement = request.sql.encode(cls.ENCODING)

        conn = await PayloadPostgres.connect(request.connection)

        try:
            async with conn, conn.transaction(), conn.cursor() as cur:
                await cls._read_copy(cur, source, statement)
                trailer = cls._copied(cur, PgCopyDirection.FROM_STDIN)
        except psycopg.Error as e:
            # текст СУБД несёт сбойную строку входа: в конверт едут тип и код
            msg = f"copy failed: {PgErrorText.of(e)}"
            raise PayloadError(FailureKind.SQL_FAILED, msg) from e

        return trailer

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
    async def _read_copy(
        cls,
        cur: psycopg.AsyncCursor[Any],
        source: BinaryIO,
        statement: bytes,
    ) -> None:
        """Вход трактуется байтами: разбор данных остаётся за самим COPY."""
        async with cur.copy(statement) as copy_in:
            while True:
                block = source.read(cls.BLOCK_BYTES)
                if not block:
                    return

                await copy_in.write(block)

    @classmethod
    def _copied(
        cls,
        cur: psycopg.AsyncCursor[Any],
        direction: PgCopyDirection,
    ) -> PgCopyTrailer:
        """Число строк оператора; psycopg ставит его после закрытия COPY."""
        # -1 у psycopg значит «счётчика нет»: для квитанции это ноль строк
        rows = max(cur.rowcount, 0)

        return PgCopyTrailer(direction=direction, rows=rows)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(PostgresOps))

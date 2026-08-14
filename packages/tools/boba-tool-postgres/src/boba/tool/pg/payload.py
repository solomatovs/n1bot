"""Операции postgres: соединение и запрос идут из песочницы.

Учётные данные едут через stdin — не видны ни в argv, ни в /proc, ни в логах.

Ошибки: PostgresError — до базы не достучаться, и sql_failed из PayloadError —
СУБД отклонила запрос; обе объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import codecs
import sys
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError
from boba.toolkit.sql import SqlEmit, SqlQueryTrailer, SqlRows


class PostgresOps:
    """Исполнение SQL; вызывается диспетчером payload'а по имени операции."""

    OPS: ClassVar[tuple[str, ...]] = ("pg_query", "pg_copy")

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        PostgresError: "database_unavailable",
    }

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        op = request["op"]
        if op == "pg_query":
            return await cls.query(request, emit)
        if op == "pg_copy":
            return await cls.copy(request, emit)
        msg = f"unknown postgres op: {op!r}"
        raise ValueError(msg)

    @classmethod
    async def query(cls, request: dict[str, Any], emit: ChunkEmitter) -> dict[str, Any]:
        """Запрос с лимитом: строки кадрами, либо счётчик, если выборки не было."""
        limit = request["row_limit"]
        params = request["params"]
        if not params:
            params = None

        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(request["sql"], params)
                if cur.description is None:
                    return cls._affected(cur).model_dump()
                truncated = await SqlEmit.rows(cls._rows(cur), emit, limit)
            except psycopg.Error as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e

            trailer = SqlQueryTrailer(
                truncated=truncated,
                returns_rows=True,
                rowcount=None,
                status=cur.statusmessage,
            )
        return trailer.model_dump()

    @classmethod
    async def _rows(
        cls, cur: psycopg.AsyncCursor[DictRow]
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Строки курсора -> записи с JSON-совместимыми значениями."""
        async for row in cur:
            yield SqlRows.of_mapping(row)

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
    async def copy(cls, request: dict[str, Any], emit: ChunkEmitter) -> dict[str, Any]:
        """COPY ... TO STDOUT: блоки уходят кадрами сразу, потолок по байтам."""
        statement = f"COPY ({request['sql']}) TO STDOUT WITH (FORMAT TEXT, HEADER)"
        max_bytes = request["max_bytes"]
        # инкрементальный декодер: блоки COPY режут utf-8 в произвольном месте
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        size = 0
        truncated = False
        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor() as cur:
            try:
                async with cur.copy(statement) as copy_out:  # type: ignore[arg-type]
                    async for block in copy_out:
                        data = bytes(block)
                        if size + len(data) > max_bytes:
                            data = data[: max_bytes - size]
                            truncated = True
                        size += len(data)
                        text = decoder.decode(data)
                        if text:
                            emit(text)
                        if truncated:
                            break
            except psycopg.Error as e:
                msg = f"copy failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e
        tail = decoder.decode(b"", True)
        if tail:
            emit(tail)
        return {"truncated": truncated}


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(PostgresOps))

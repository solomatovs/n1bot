"""Операции postgres: соединение и запрос идут из песочницы.

Учётные данные едут через stdin — не видны ни в argv, ни в /proc, ни в логах.

Ошибки: PostgresError — до базы не достучаться, и sql_failed из PayloadError —
СУБД отклонила запрос; обе объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import codecs
import sys
from collections.abc import Mapping
from typing import Any, ClassVar

import psycopg
from psycopg.rows import dict_row

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError


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
        """Запрос с лимитом: строки уходят кадрами, лишняя строка ловит усечение."""
        limit = request["row_limit"]
        params = request["params"]
        if not params:
            params = None
        emitted = 0
        truncated = False
        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(request["sql"], params)
                async for row in cur:
                    if emitted >= limit:
                        truncated = True
                        break
                    emit(RowStream.encode(PayloadPostgres.jsonable(row)))
                    emitted += 1
            except psycopg.Error as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e
        return {"truncated": truncated}

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

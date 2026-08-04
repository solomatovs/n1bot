"""Операции postgres: соединение и запрос идут из песочницы.

Учётные данные едут через stdin — не видны ни в argv, ни в /proc, ни в логах.
"""

from __future__ import annotations

import sys
from typing import Any, ClassVar

import psycopg
from psycopg.rows import dict_row

from boba.db.postgres import PayloadPostgres
from boba.toolkit.payload import PayloadEntry


class PostgresOps:
    """Исполнение SQL; вызывается диспетчером payload'а по имени операции."""

    OPS: ClassVar[tuple[str, ...]] = ("pg_query", "pg_copy")

    @classmethod
    async def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == "pg_query":
            return await cls.query(request)
        if op == "pg_copy":
            return await cls.copy(request)
        msg = f"unknown postgres op: {op!r}"
        raise ValueError(msg)

    @classmethod
    async def query(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Запрос с лимитом строк: лишняя строка ловит факт усечения."""
        limit = request["row_limit"]
        fetch = limit + 1
        params = request["params"]
        if not params:
            params = None
        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(request["sql"], params)
                fetched = await cur.fetchmany(fetch)
            except psycopg.Error as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise RuntimeError(msg) from e
        truncated = len(fetched) > limit
        rows: list[dict[str, Any]] = []
        for row in fetched[:limit]:
            rows.append(PayloadPostgres.jsonable(row))
        return {"rows": rows, "truncated": truncated}

    @classmethod
    async def copy(cls, request: dict[str, Any]) -> dict[str, Any]:
        """COPY ... TO STDOUT: текстовая выгрузка с потолком по байтам."""
        statement = f"COPY ({request['sql']}) TO STDOUT WITH (FORMAT TEXT, HEADER)"
        max_bytes = request["max_bytes"]
        chunks: list[bytes] = []
        size = 0
        truncated = False
        conn = await PayloadPostgres.connect(request)
        async with conn, conn.cursor() as cur:
            try:
                async with cur.copy(statement) as copy_out:  # type: ignore[arg-type]
                    async for block in copy_out:
                        data = bytes(block)
                        if size + len(data) > max_bytes:
                            chunks.append(data[: max_bytes - size])
                            truncated = True
                            break
                        chunks.append(data)
                        size += len(data)
            except psycopg.Error as e:
                msg = f"copy failed: {type(e).__name__}: {e}"
                raise RuntimeError(msg) from e
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return {"text": text, "truncated": truncated}


if __name__ == "__main__":
    sys.exit(PayloadEntry.main_async(PostgresOps.dispatch))

"""Операции postgres: соединение и запрос идут из песочницы.

Приложение отдаёт сюда libpq-параметры соединения и готовый SQL, обратно
получает строки. Пула здесь нет: каждый вызов — свой процесс, а значит своё
соединение; цена — рукопожатие (и kerberos) на каждый запрос.

Учётные данные приезжают через stdin, поэтому не видны ни в argv, ни в
/proc, ни в логах приложения.
"""

from __future__ import annotations

from typing import Any, ClassVar

import psycopg
from psycopg.rows import dict_row


class PostgresOps:
    """Исполнение SQL; вызывается диспетчером payload'а по имени операции."""

    OPS: ClassVar[tuple[str, ...]] = ("pg_query", "pg_copy")

    @classmethod
    def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == "pg_query":
            return cls.query(request)
        if op == "pg_copy":
            return cls.copy(request)
        msg = f"unknown postgres op: {op!r}"
        raise ValueError(msg)

    @staticmethod
    def connect(request: dict[str, Any]) -> psycopg.Connection[Any]:
        settings = dict(request["connection"])
        try:
            return psycopg.connect(**settings)
        except psycopg.Error as e:
            msg = f"connect failed: {type(e).__name__}: {e}"
            raise RuntimeError(msg) from e

    @classmethod
    def query(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Запрос с лимитом строк: лишняя строка ловит факт усечения."""
        limit = request["row_limit"]
        fetch = limit + 1
        params = request["params"]
        if not params:
            params = None
        with cls.connect(request) as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(request["sql"], params)
                fetched = cur.fetchmany(fetch)
            except psycopg.Error as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise RuntimeError(msg) from e
        truncated = len(fetched) > limit
        rows: list[dict[str, Any]] = []
        for row in fetched[:limit]:
            rows.append(cls.jsonable(row))
        return {"rows": rows, "truncated": truncated}

    @classmethod
    def copy(cls, request: dict[str, Any]) -> dict[str, Any]:
        """COPY ... TO STDOUT: текстовая выгрузка с потолком по байтам."""
        statement = f"COPY ({request['sql']}) TO STDOUT WITH (FORMAT TEXT, HEADER)"
        max_bytes = request["max_bytes"]
        chunks: list[bytes] = []
        size = 0
        truncated = False
        with cls.connect(request) as conn, conn.cursor() as cur:
            try:
                with cur.copy(statement) as copy_out:  # type: ignore[arg-type]
                    for block in copy_out:
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

    @classmethod
    def jsonable(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Decimal/UUID/datetime -> строки: JSON другого не умеет."""
        out: dict[str, Any] = {}
        for name, value in row.items():
            out[name] = cls.scalar(value)
        return out

    @classmethod
    def scalar(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (list, tuple)):
            items: list[Any] = []
            for item in value:
                items.append(cls.scalar(item))
            return items
        if isinstance(value, dict):
            mapping: dict[str, Any] = {}
            for key, item in value.items():
                mapping[str(key)] = cls.scalar(item)
            return mapping
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)

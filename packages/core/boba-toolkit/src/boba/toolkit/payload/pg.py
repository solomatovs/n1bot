"""Postgres для payload'ов; пула нет — каждый вызов свой процесс и соединение.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах."""

from __future__ import annotations

from typing import Any

import psycopg

__all__ = ["PayloadPostgres"]


class PayloadPostgres:
    """Подключение по libpq-параметрам запроса и JSON-совместимые строки."""

    @staticmethod
    def connect(request: dict[str, Any]) -> psycopg.Connection[Any]:
        settings = dict(request["connection"])
        try:
            return psycopg.connect(**settings)
        except psycopg.Error as e:
            msg = f"connect failed: {type(e).__name__}: {e}"
            raise RuntimeError(msg) from e

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

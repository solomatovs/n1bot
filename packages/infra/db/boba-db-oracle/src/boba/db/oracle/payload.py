"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import oracledb
from oracledb import AsyncConnection, AsyncCursor

from boba.db.oracle.errors import OracleError, OracleQueryError
from boba.db.oracle.profile import OracleConfig

__all__ = ["PayloadOracle", "RowStream"]


@dataclass(frozen=True, slots=True)
class RowStream:
    """Строки одного запроса: имена колонок и сами строки асинхронным потоком.

    Драйвер отдаёт имена колонок отдельно от значений, поэтому они едут вместе
    с потоком: вызывающий собирает словарь строки по names, не заглядывая во
    внутренности курсора. Имена в нижнем регистре: Oracle хранит их заглавными.
    """

    names: tuple[str, ...]
    blocks: AsyncIterator[Sequence[Any]]


class PayloadOracle:
    """Соединение по профилю и строки запроса на нём.

    Пула нет: скрапер держит одно соединение на попытку, payload — на вызов.
    LOB-колонки читаются строками и байтами, а не объектами LOB, чтобы поток
    строк не зависел от открытого курсора; NUMBER приходит Decimal, а не float:
    битовые поля словаря шире 2^53.
    """

    ARRAYSIZE: ClassVar[int] = 2000

    @staticmethod
    @asynccontextmanager
    async def opened_config(
        connection: OracleConfig,
    ) -> AsyncGenerator[AsyncConnection, None]:
        """Соединение на время операции; закрывается на выходе из блока."""
        oracledb.defaults.fetch_lobs = False
        oracledb.defaults.fetch_decimals = True

        try:
            conn = await oracledb.connect_async(**connection.connect_settings())
        except oracledb.Error as exc:
            raise OracleError(
                f"connecting to oracle {connection.where()} as {connection.trace()}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        conn.call_timeout = connection.call_timeout
        try:
            yield conn
        finally:
            await conn.close()

    @staticmethod
    @asynccontextmanager
    async def rows(
        conn: AsyncConnection,
        text: str,
        parameters: Mapping[str, object] | None = None,
    ) -> AsyncGenerator[RowStream, None]:
        """Строки запроса на открытом соединении: одна сессия на много запросов.
        Именованные bind'ы `:name`, только скалярные значения: коллекции thin-режим
        читает не в каждой кодировке базы."""
        binds: dict[str, Any] = {}
        if parameters:
            binds = dict(parameters)

        cursor = conn.cursor()
        cursor.arraysize = PayloadOracle.ARRAYSIZE

        try:
            await cursor.execute(text, binds)
        except oracledb.Error as exc:
            cursor.close()
            raise OracleQueryError(
                f"query on oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

        names: list[str] = []
        for column in cursor.description or ():
            names.append(str(column[0]).lower())

        try:
            yield RowStream(
                names=tuple(names), blocks=PayloadOracle._iterate(cursor, text)
            )
        finally:
            cursor.close()

    @staticmethod
    async def _iterate(cursor: AsyncCursor, text: str) -> AsyncIterator[Sequence[Any]]:
        try:
            async for row in cursor:
                yield row
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"reading rows from oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

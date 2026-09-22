"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами или CSV-байтами пачками.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import oracledb
from oracledb import AsyncConnection, AsyncCursor

from boba.db.oracle.errors import OracleError, OracleQueryError
from boba.db.oracle.profile import OracleConfig

__all__ = ["ByteStream", "PayloadOracle", "RowStream"]


@dataclass(frozen=True, slots=True)
class RowStream:
    """Строки одного запроса: имена колонок и сами строки асинхронным потоком.

    Драйвер отдаёт имена колонок отдельно от значений, поэтому они едут вместе
    с потоком: вызывающий собирает словарь строки по names, не заглядывая во
    внутренности курсора. Имена в нижнем регистре: Oracle хранит их заглавными.
    """

    names: tuple[str, ...]
    blocks: AsyncIterator[Sequence[Any]]


@dataclass(frozen=True)
class ByteStream:
    """Ответ запроса байтами CSV без заголовка: имена колонок и блоки по пачкам
    arraysize строк."""

    names: tuple[str, ...]
    blocks: AsyncIterator[memoryview]


class PayloadOracle:
    """Соединение по профилю и запросы на нём: строки потоком или CSV-байты пачками.

    Создаётся на профиль OracleConfig; из него берутся параметры соединения,
    call_timeout и arraysize курсоров. Пула нет: скрапер держит одно соединение на
    попытку, payload — на вызов. LOB-колонки читаются строками и байтами, а не
    объектами LOB, чтобы поток строк не зависел от открытого курсора; NUMBER
    приходит Decimal, а не float: битовые поля словаря шире 2^53.
    """

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, connection: OracleConfig) -> None:
        self._connection = connection

    @asynccontextmanager
    async def opened(self) -> AsyncGenerator[AsyncConnection, None]:
        """Соединение на время операции; закрывается на выходе из блока."""
        oracledb.defaults.fetch_lobs = False
        oracledb.defaults.fetch_decimals = True

        connection = self._connection
        try:
            conn = await oracledb.connect_async(**connection.connect_settings())
        except oracledb.Error as exc:
            raise OracleError(
                f"connecting to oracle {connection.address_prefix()} "
                f"as {connection.trace()}: {type(exc).__name__}: {exc}"
            ) from exc

        conn.call_timeout = connection.call_timeout
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def rows(
        self,
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

        cursor = await self._executed(conn, text, binds)
        try:
            yield RowStream(
                names=self._names(cursor),
                blocks=self._iterate(cursor, text),
            )
        finally:
            cursor.close()

    @asynccontextmanager
    async def csv(
        self, conn: AsyncConnection, text: str
    ) -> AsyncGenerator[ByteStream, None]:
        """Ответ запроса CSV-байтами без заголовка пачками по arraysize: пачку строк
        собирает драйвер, в текст её переводит csv.writer одним вызовом. NULL это
        пустое поле, строка с кавычкой, запятой или переводом строки — в кавычках,
        NUMBER пишется как Decimal, DATE и TIMESTAMP — ISO с пробелом. RAW запрос
        отдаёт `rawtohex`: bytes в CSV не пишутся. Ответ Arrow (fetch_df_batches)
        не используется: на 12.2 и 18 он роняет thin-драйвер 26.0 на обычных
        запросах словаря."""
        cursor = await self._executed(conn, text, {})
        try:
            yield ByteStream(
                names=self._names(cursor),
                blocks=self._csv_batches(cursor, text),
            )
        finally:
            cursor.close()

    async def _executed(
        self, conn: AsyncConnection, text: str, binds: Mapping[str, Any]
    ) -> AsyncCursor:
        cursor = conn.cursor()
        cursor.arraysize = self._connection.arraysize

        try:
            await cursor.execute(text, dict(binds))
        except oracledb.Error as exc:
            cursor.close()
            raise OracleQueryError(
                f"query on oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

        return cursor

    @staticmethod
    def _names(cursor: AsyncCursor) -> tuple[str, ...]:
        names: list[str] = []
        for column in cursor.description or ():
            names.append(str(column[0]).lower())

        return tuple(names)

    @classmethod
    async def _csv_batches(
        cls, cursor: AsyncCursor, text: str
    ) -> AsyncIterator[memoryview]:
        try:
            while True:
                rows = await cursor.fetchmany()
                if not rows:
                    return

                buffer = io.StringIO()
                csv.writer(buffer, lineterminator="\n").writerows(rows)
                yield memoryview(buffer.getvalue().encode(cls.ENCODING))
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"reading rows from oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

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

"""Курсор с логом запросов: в лог уходит SQL с подставленными параметрами.

Обычный курсор psycopg биндит параметры на сервере, и готового текста запроса
на клиенте не существует — логировать нечего. Здесь параметры подставляет
клиент (AsyncClientCursor), поэтому в лог попадает ровно тот текст, который
отправляется серверу. Ставится фабрикой курсоров на соединении, вызывающий код
не меняется.

Ошибки:
psycopg.Error — запрос отклонён сервером либо параметр не адаптируется;
    поведение курсора не меняется и своих ошибок он не добавляет.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, Self

import psycopg
from psycopg import AsyncClientCursor
from psycopg.abc import Params, QueryNoTemplate
from psycopg.copy import AsyncCopy, AsyncWriter
from psycopg.rows import Row

__all__ = ["LoggingCursor"]

logger = logging.getLogger(__name__)


class LoggingCursor(AsyncClientCursor[Row]):
    """
    Курсор psycopg, пишущий в лог каждый запрос перед отправкой на сервер.
    """

    @classmethod
    def attach(cls, conn: psycopg.AsyncConnection[Any]) -> None:
        """Поставить курсор фабрикой соединения: его курсоры логируют запросы."""
        conn.cursor_factory = cls

    async def execute(
        self,
        query: QueryNoTemplate,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Self:
        logger.info("pg query: %s", self.mogrify(query, params))

        return await super().execute(query, params, prepare=prepare, binary=binary)

    async def executemany(
        self,
        query: QueryNoTemplate,
        params_seq: Iterable[Params],
        *,
        returning: bool = False,
    ) -> None:
        # последовательность материализуется: её же читает лог и сам executemany
        batch = list(params_seq)

        for params in batch:
            logger.info("pg query: %s", self.mogrify(query, params))

        await super().executemany(query, batch, returning=returning)

    def stream(
        self,
        query: QueryNoTemplate,
        params: Params | None = None,
        *,
        binary: bool | None = None,
        size: int = 1,
    ) -> AsyncIterator[Row]:
        logger.info("pg query: %s", self.mogrify(query, params))

        return super().stream(query, params, binary=binary, size=size)

    @asynccontextmanager
    async def copy(
        self,
        statement: QueryNoTemplate,
        params: Params | None = None,
        *,
        writer: AsyncWriter | None = None,
    ) -> AsyncGenerator[AsyncCopy]:
        logger.info("pg query: %s", self.mogrify(statement, params))

        async with super().copy(statement, params, writer=writer) as copy:
            yield copy

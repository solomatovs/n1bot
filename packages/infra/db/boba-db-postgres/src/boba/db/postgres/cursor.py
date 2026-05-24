from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Self

import psycopg
from psycopg.abc import Params, Query, QueryNoTemplate
from psycopg.copy import Copy, Writer
from psycopg.rows import Row

logger = logging.getLogger(__name__)


class LogSqlCursor(psycopg.Cursor[Row]):
    """Курсор, логирующий каждый SQL/COPY через `logger` (psycopg3)."""

    def execute(
        self,
        query: QueryNoTemplate,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> Self:
        dsn = self.connection.info.dsn
        logger.debug("execute dsn=%s sql=%s params=%r", dsn, query, params)
        try:
            return super().execute(query, params, prepare=prepare, binary=binary)
        except Exception:
            logger.exception(
                "execute failed dsn=%s sql=%s params=%r", dsn, query, params
            )
            raise

    def executemany(
        self,
        query: Query,
        params_seq: Sequence[Params],
        *,
        returning: bool = False,
    ) -> None:
        dsn = self.connection.info.dsn
        logger.debug("executemany dsn=%s sql=%s n=%d", dsn, query, len(params_seq))
        try:
            super().executemany(query, params_seq, returning=returning)
        except Exception:
            logger.exception(
                "executemany failed dsn=%s sql=%s n=%d", dsn, query, len(params_seq)
            )
            raise

    @contextmanager
    def copy(
        self,
        statement: Query,
        params: Params | None = None,
        *,
        writer: Writer | None = None,
    ) -> Iterator[Copy]:
        dsn = self.connection.info.dsn
        logger.debug("copy dsn=%s sql=%s params=%r", dsn, statement, params)
        try:
            with super().copy(statement, params, writer=writer) as cp:
                yield cp
        except Exception:
            logger.exception(
                "copy failed dsn=%s sql=%s params=%r", dsn, statement, params
            )
            raise

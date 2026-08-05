"""Вызов postgres-payload'а: соединение и запрос идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from boba.db.postgres import PostgresConfig
from boba.tool.pg.protocol import (
    PgCopyRequest,
    PgCopyTrailer,
    PgQueryRequest,
    PgQueryTrailer,
)
from boba.toolkit.launcher import ChunkSink, LauncherFactory

__all__ = ["PgCaller"]


class PgCaller:
    """Один вызов payload'а на запрос; пул не переживает вызов по построению."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.pg.payload")

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def query(
        self,
        *,
        connection: PostgresConfig,
        sql: str,
        params: Sequence[Any],
        row_limit: int,
        sink: ChunkSink,
    ) -> PgQueryTrailer:
        request = PgQueryRequest(
            op=PgQueryRequest.OP,
            connection=connection,
            sql=sql,
            params=tuple(params),
            row_limit=row_limit,
        )
        return self._caller.call_stream(self.ENTRY, request, sink, PgQueryTrailer)

    def copy(
        self,
        *,
        connection: PostgresConfig,
        sql: str,
        max_bytes: int,
        sink: ChunkSink,
    ) -> PgCopyTrailer:
        request = PgCopyRequest(
            op=PgCopyRequest.OP,
            connection=connection,
            sql=sql,
            max_bytes=max_bytes,
        )
        return self._caller.call_stream(self.ENTRY, request, sink, PgCopyTrailer)

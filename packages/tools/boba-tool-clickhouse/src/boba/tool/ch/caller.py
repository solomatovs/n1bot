"""Вызов clickhouse-payload'а: соединение и запрос идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from boba.db.clickhouse import ClickHouseConfig
from boba.tool.ch.protocol import ChQueryRequest, ChQueryTrailer
from boba.toolkit.launcher import ChunkSink, LauncherFactory

__all__ = ["ChCaller"]


class ChCaller:
    """Один вызов payload'а на запрос; клиент не переживает вызов по построению."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.ch.payload")

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def query(
        self,
        *,
        connection: ClickHouseConfig,
        sql: str,
        params: Mapping[str, Any],
        row_limit: int,
        sink: ChunkSink,
    ) -> ChQueryTrailer:
        request = ChQueryRequest(
            op=ChQueryRequest.OP,
            connection=connection,
            sql=sql,
            params=dict(params),
            row_limit=row_limit,
        )
        return self._caller.call_stream(self.ENTRY, request, sink, ChQueryTrailer)

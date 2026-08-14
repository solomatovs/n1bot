"""Операции clickhouse: соединение и запрос идут из песочницы.

Учётные данные едут через stdin — не видны ни в argv, ни в /proc, ни в логах.

Ошибки: ClickHouseError — до базы не достучаться, и sql_failed из PayloadError —
СУБД отклонила запрос; обе объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, cast

from clickhouse_connect.driver.common import StreamContext
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse import ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError
from boba.toolkit.sql import SqlEmit, SqlQueryTrailer, SqlRows


class ClickHouseOps:
    """Исполнение SQL; вызывается диспетчером payload'а по имени операции."""

    OPS: ClassVar[tuple[str, ...]] = ("ch_query",)

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        ClickHouseError: "database_unavailable",
    }

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        op = request["op"]
        if op == "ch_query":
            return await cls.query(request, emit)
        msg = f"unknown clickhouse op: {op!r}"
        raise ValueError(msg)

    @classmethod
    async def query(cls, request: dict[str, Any], emit: ChunkEmitter) -> dict[str, Any]:
        """Запрос с лимитом: блоки строк уходят кадрами по мере прихода с сервера."""
        params = request["params"]
        if not params:
            params = None

        async with PayloadClickHouse.opened(request) as client:
            try:
                stream = await client.query_row_block_stream(
                    request["sql"],
                    parameters=params,
                )
                async with stream as blocks:
                    names: Sequence[str] = cast(Any, blocks.source).column_names
                    rows = cls._rows(blocks, names)
                    truncated = await SqlEmit.rows(rows, emit, request["row_limit"])
            except DriverError as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e

        return cls._trailer(truncated).model_dump()

    @classmethod
    async def _rows(
        cls, blocks: StreamContext, names: Sequence[str]
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Блоки кортежей -> записи-словари с JSON-совместимыми значениями."""
        async for block in blocks:
            for row in cast(Sequence[Sequence[Any]], block):
                yield SqlRows.of_columns(names, row)

    @classmethod
    def _trailer(cls, truncated: bool) -> SqlQueryTrailer:
        """Итог запроса: у этой операции всегда выборка, счётчика и статуса нет.

        По колонкам о выборке судить нельзя: пустой результат приходит без
        схемы. Счётчика тоже нет — summary отдаёт result_rows на момент старта
        стрима, а не итог чтения.
        """
        return SqlQueryTrailer(
            truncated=truncated,
            returns_rows=True,
            rowcount=None,
            status=None,
        )


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ClickHouseOps))

"""Операции clickhouse: соединение и запрос идут из песочницы.

Учётные данные едут через stdin — не видны ни в argv, ни в /proc, ни в логах.

Ошибки: ClickHouseError — до базы не достучаться, и sql_failed из PayloadError —
СУБД отклонила запрос; обе объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, cast

from clickhouse_connect.driver.common import StreamContext
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse import ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError


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
                    truncated = await cls._emit_rows(blocks, emit, request["row_limit"])
            except DriverError as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError("sql_failed", msg) from e
        return {"truncated": truncated}

    @classmethod
    async def _emit_rows(
        cls, blocks: StreamContext, emit: ChunkEmitter, limit: int
    ) -> bool:
        """Кадр на строку; лишняя строка сверх лимита ловит усечение и рвёт поток."""
        names: Sequence[str] = cast(Any, blocks.source).column_names
        emitted = 0
        async for block in blocks:
            for row in cast(Sequence[Sequence[Any]], block):
                if emitted >= limit:
                    return True
                emit(RowStream.encode(PayloadClickHouse.jsonable(names, row)))
                emitted += 1
        return False


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ClickHouseOps))

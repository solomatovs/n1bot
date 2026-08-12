"""Операции clickhouse: соединение, запрос и вставка идут из песочницы.

Учётные данные приезжают каналом tool_args — не видны ни в argv, ни в /proc,
ни в логах; строки уходят каналом данных, вход вставки читается из tool_stdin.

Ошибки: ClickHouseError — до базы не достучаться; PayloadError видов
FailureKind (sql_failed — СУБД отклонила запрос, no_input — вставке не подали
поток); все объявлены ожидаемыми и едут пользователю текстом.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.common import StreamContext
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from clickhouse_connect.driver.query import QueryResult
from pydantic import BaseModel

from boba.db.clickhouse import ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.tool.ch.protocol import (
    ChInsertRequest,
    ChInsertTrailer,
    ChQueryRequest,
    ChQueryTrailer,
    ChStage,
    ChWireFormat,
)
from boba.toolkit.channels import Channel, StreamCodec
from boba.toolkit.payload import (
    FailureKind,
    PayloadChannels,
    PayloadEntry,
    PayloadError,
    PayloadOutputClosedError,
    PayloadStream,
)


class ClickHouseOps:
    """Исполнение SQL; операцию выбирает реестр запросов по полю op."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        ClickHouseError: FailureKind.UNAVAILABLE,
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        ChStage.QUERY: ChQueryRequest,
        ChStage.INSERT: ChInsertRequest,
    }

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        if isinstance(request, ChQueryRequest):
            return await cls.query(request, channels)

        if isinstance(request, ChInsertRequest):
            return await cls.insert(request, channels)

        msg = f"unsupported clickhouse request: {type(request).__name__}"
        raise TypeError(msg)

    @classmethod
    async def query(
        cls, request: ChQueryRequest, channels: PayloadChannels
    ) -> ChQueryTrailer:
        """Запрос с лимитом: блоки строк уходят в канал данных по мере прихода."""
        params = request.params
        if not params:
            params = None

        stream = channels.payload()

        async with PayloadClickHouse.opened(request.connection) as client:
            try:
                truncated = await cls._stream_rows(
                    client, request, params, stream
                )
            except PayloadOutputClosedError:
                # потребитель закрыл чтение: строки больше не нужны, квитанция едет
                truncated = True
            except DriverError as e:
                msg = f"query failed: {type(e).__name__}: {e}"
                raise PayloadError(FailureKind.SQL_FAILED, msg) from e

        return ChQueryTrailer(truncated=truncated)

    @classmethod
    async def _stream_rows(
        cls,
        client: AsyncClient,
        request: ChQueryRequest,
        params: dict[str, Any] | None,
        stream: PayloadStream,
    ) -> bool:
        blocks = await client.query_row_block_stream(request.sql, parameters=params)

        async with blocks as source:
            return await cls._write_rows(source, stream, request.row_limit)

    @classmethod
    async def _write_rows(
        cls, blocks: StreamContext, stream: PayloadStream, limit: int
    ) -> bool:
        """Строка на запись; строка сверх лимита ловит усечение и рвёт поток."""
        names = cls._column_names(blocks)

        written = 0
        async for block in blocks:
            # драйвер объявляет блок потока опциональным: пустого блока не ждём
            if block is None:
                continue

            rows: Sequence[Sequence[Any]] = block
            for row in rows:
                if written >= limit:
                    return True

                jsonable = PayloadClickHouse.jsonable(names, row)
                stream.write(StreamCodec.encode_row(jsonable))
                written += 1

        return False

    @staticmethod
    def _column_names(blocks: StreamContext) -> Sequence[str]:
        """Имена колонок живут в источнике потока; иной источник — не наш контракт."""
        source = blocks.source

        if not isinstance(source, QueryResult):
            msg = f"clickhouse block stream source is {type(source).__name__}"
            raise TypeError(msg)

        return source.column_names

    @classmethod
    async def insert(
        cls, request: ChInsertRequest, channels: PayloadChannels
    ) -> ChInsertTrailer:
        """Вставка входного потока в таблицу; формат объявлен полем запроса."""
        if not channels.has(Channel.TOOL_STDIN):
            raise PayloadError(
                FailureKind.NO_INPUT, "insert source is not fed to the stage"
            )

        wire = ChWireFormat.of(request.stdin_format)

        source = channels.stdin()

        async with PayloadClickHouse.opened(request.connection) as client:
            try:
                summary = await client.raw_insert(
                    table=request.table,
                    insert_block=source,
                    fmt=wire.value,
                )
            except DriverError as e:
                msg = f"insert failed: {type(e).__name__}: {e}"
                raise PayloadError(FailureKind.SQL_FAILED, msg) from e

        return ChInsertTrailer(
            rows=summary.written_rows,
            bytes_written=summary.written_bytes(),
        )


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ClickHouseOps))

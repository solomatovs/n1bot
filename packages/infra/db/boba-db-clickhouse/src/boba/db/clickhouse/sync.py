"""Sync-семейство ClickHouse: источник ChSyncSource — ответ запроса в
FORMAT ArrowStream от сервера читается pyarrow только ради схемы и
контракта, пачки перекладываются в кадры потока тем же IPC без разбора
значений; декларации вызова ложатся поверх типов Arrow.

Ошибки:
SyncError — декларация на колонку, которой нет в ответе.
ClickHouseQueryError — сервер отклонил запрос или ответ оборвался.
ArrowStreamError — ответ сервера не читается как поток Arrow IPC.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse.payload import PayloadClickHouse, ReadTuning
from boba.db.clickhouse.trace import ChCommandReport
from boba.toolkit.arrow import ArrowColumns, ArrowIpc, BytePipe
from boba.toolkit.sync import (
    ArrowContract,
    ColumnDeclaration,
    ColumnSpec,
    ContractText,
    Declarations,
    StreamContract,
)
from boba.toolkit.transfer import Engine, SchemaHead, StreamWire, TransferOutbound

__all__ = ["ChSyncSource"]


class ChSyncSource:
    """Источник sync-потока ClickHouse: всегда Arrow IPC — контракт из схемы
    ответа сервера, тела кадров — те же пачки, переписанные писателем IPC."""

    ENGINE: ClassVar[Engine] = Engine.CLICKHOUSE
    FORMAT: ClassVar[str] = "ArrowStream"

    def __init__(self, client: AsyncClient) -> None:
        self._client = client
        self._payload = PayloadClickHouse
        self._ipc = ArrowIpc()
        self._columns = ArrowColumns()
        self._declarations = Declarations()
        self._contract = StreamContract()
        self._contract_text = ContractText()

    async def stream(
        self,
        text: str,
        declared: Sequence[ColumnDeclaration],
        chunk_bytes: int,
        out: TransferOutbound,
    ) -> ChCommandReport:
        pipe = BytePipe()
        tuning = ReadTuning(socket_read_size=chunk_bytes, read_buffer_size=chunk_bytes)
        async with self._payload.byte_stream_out(
            self._client, text, self.FORMAT, tuning=tuning
        ) as stream:

            async def produce() -> None:
                try:
                    async for block in stream.blocks:
                        await pipe.write(block)
                finally:
                    pipe.close_write()

            async def consume() -> tuple[ColumnSpec, ...]:
                try:
                    reader = await self._ipc.open_in(pipe.source, chunk_bytes)
                    specs = self._declarations.merge(
                        self._columns.specs(reader.schema), declared
                    )
                    contract = ArrowContract(columns=self._contract.columns(specs))
                    await out.schema(
                        SchemaHead(
                            kind="schema",
                            source_engine=self.ENGINE,
                            wire=StreamWire.ARROW,
                            contract=contract.model_dump(mode="json"),
                        )
                    )
                    writer = await self._ipc.open_out(out.writer(), reader.schema)
                    async for batch in reader.batches:
                        await writer.write(batch)

                    await writer.close()

                    return specs
                finally:
                    pipe.close_read()

            _, specs = await asyncio.gather(produce(), consume())

            return stream.trace.report(
                self._contract_text.render(StreamWire.ARROW.value, specs), text
            )

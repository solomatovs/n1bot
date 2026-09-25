"""Arrow-порты toolkit поверх трубы ОС: ArrowOutbound пишет схему и пачки,
ArrowInbound на другом конце читает ту же схему и те же пачки, а поток на
проводе — обычный Arrow IPC, который читает и pyarrow напрямую; мусор на
входе — ArrowStreamError. Баз стенда не трогает."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pyarrow
import pyarrow.ipc
import pytest

from boba.pump_stand import Pipe
from boba.toolkit.arrow import ArrowInbound, ArrowOutbound, ArrowStreamError
from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound

pytestmark = pytest.mark.anyio

BUFFER = 4096
SCHEMA = pyarrow.schema(
    [
        pyarrow.field("id", pyarrow.int64()),
        pyarrow.field("amount", pyarrow.decimal128(18, 4)),
        pyarrow.field("note", pyarrow.large_string()),
        pyarrow.field("bin", pyarrow.large_binary()),
    ]
)


def _batch(start: int, rows: int) -> pyarrow.RecordBatch:
    ids = list(range(start, start + rows))
    return pyarrow.record_batch(
        [
            pyarrow.array(ids, pyarrow.int64()),
            pyarrow.array([f"{n}.1234" for n in ids]).cast(pyarrow.decimal128(18, 4)),
            pyarrow.array([f"tab\t{n}\nline" for n in ids], pyarrow.large_string()),
            pyarrow.array(
                [bytes([n % 256, 0, 255]) for n in ids], pyarrow.large_binary()
            ),
        ],
        schema=SCHEMA,
    )


class TestArrowPorts:
    async def test_batches_cross_the_pipe_intact(self) -> None:
        pipe = Pipe(ArrowOutbound, ArrowInbound)
        sent = [_batch(0, 1000), _batch(1000, 7), _batch(1007, 3000)]

        async def produce() -> None:
            out = pipe.outbound
            if not isinstance(out, ArrowOutbound):
                raise AssertionError("outbound port is ArrowOutbound")

            try:
                writer = await out.open(SCHEMA)
                for batch in sent:
                    await writer.write(batch)

                await writer.close()
            finally:
                pipe.close_write()

        async def consume() -> list[Any]:
            feed = pipe.inbound
            if not isinstance(feed, ArrowInbound):
                raise AssertionError("inbound port is ArrowInbound")

            try:
                reader = await feed.open(BUFFER)
                assert reader.schema.equals(SCHEMA)

                return [batch async for batch in reader.batches]
            finally:
                pipe.close_read()

        _, received = await asyncio.gather(produce(), consume())

        assert pyarrow.Table.from_batches(received).equals(
            pyarrow.Table.from_batches(sent)
        )

    async def test_wire_is_plain_arrow_ipc(self) -> None:
        """Сырой приёмник на другом конце читает поток обычным pyarrow."""
        read_fd, write_fd = os.pipe()
        out = ArrowOutbound(ToolIo.on_channels(-1, write_fd))

        async def produce() -> None:
            try:
                writer = await out.open(SCHEMA)
                await writer.write(_batch(0, 5))
                await writer.close()
            finally:
                os.close(write_fd)

        async def consume() -> pyarrow.Table:
            raw = RawInbound(ToolIo.on_channels(read_fd, -1))
            try:
                data = await asyncio.to_thread(raw.readall)
            finally:
                os.close(read_fd)

            return pyarrow.ipc.open_stream(data).read_all()

        _, table = await asyncio.gather(produce(), consume())

        assert table.equals(pyarrow.Table.from_batches([_batch(0, 5)]))

    async def test_garbage_is_refused_with_a_format_error(self) -> None:
        read_fd, write_fd = os.pipe()
        raw = RawOutbound(ToolIo.on_channels(-1, write_fd))
        feed = ArrowInbound(ToolIo.on_channels(read_fd, -1))

        async def produce() -> None:
            try:
                await raw.send(b"id,amount\n1,2.5\n")
            finally:
                os.close(write_fd)

        async def consume() -> None:
            try:
                with pytest.raises(ArrowStreamError, match="schema failed"):
                    await feed.open(BUFFER)
            finally:
                os.close(read_fd)

        await asyncio.gather(produce(), consume())
